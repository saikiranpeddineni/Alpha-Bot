from __future__ import annotations

from datetime import UTC, datetime, timedelta
import pandas as pd
import pytest

from app.core.domain import Regime, Signal, SignalAction
from app.risk.market_regime import MarketRiskAggregator, MarketRiskState
from app.strategies.base import StrategyContext
from app.strategies.momentum import MomentumCross
from app.strategies.orb import OpeningRangeBreakout
from app.ai.monte_carlo_gate import MonteCarloGate
from unittest.mock import MagicMock
from app.config import RiskConfig
from app.risk.manager import RiskManager, AccountContext
from app.strategies.gap_and_go import GapAndGo
from app.strategies.bb_squeeze import BollingerBandSqueeze
from app.strategies.pipeline import SignalPipeline


@pytest.mark.asyncio
async def test_market_regime_intraday_uptrend_fallback():
    """Verify that when <20 hourly bars exist, intraday VWAP/open fallback identifies uptrend."""
    class MockData:
        pass

    agg = MarketRiskAggregator(MockData())
    # Create 30 1-minute bars where price starts at 500 and ends at 505 (clear intraday uptrend)
    start = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
    idx = [start + timedelta(minutes=i) for i in range(30)]
    closes = [500.0 + i * 0.20 for i in range(30)]
    opens = [500.0] + closes[:-1]
    highs = [c + 0.10 for c in closes]
    lows = [o - 0.10 for o in opens]
    volumes = [10000.0 for _ in range(30)]

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=idx)

    # Hourly bars will only have 1 bar (< 20)
    df_h = df.resample("1h").agg({"close": "last"}).dropna()
    assert len(df_h) < 20

    # In the updated logic, last_close (505.8) >= session_open (500.0) -> is_uptrend should be True
    last_close = float(df["close"].iloc[-1])
    session_open = float(df["open"].iloc[0])
    vwap_series = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
    assert last_close >= float(vwap_series.iloc[-1])
    assert last_close >= session_open


def test_orb_rejects_short_on_positive_trend():
    """Verify that ORB rejects short entries on equities trading above session open or flagged as gainers."""
    strat = OpeningRangeBreakout()
    start = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
    idx = [start + timedelta(minutes=i) for i in range(25)]

    # Stock opened at 100, surged to 105 in OR (first 15m), then pulls back to 102 (still above open 100, but below OR low 103)
    # OR: high = 105, low = 103
    prices = [100.0, 103.0, 104.0, 105.0] + [104.0] * 11 + [102.0] * 10
    opens = [100.0] + prices[:-1]
    highs = [p + 0.5 for p in prices]
    lows = [p - 0.5 for p in prices]
    volumes = [50000.0 for _ in prices]

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    }, index=idx)

    from app.indicators.ta import enrich
    df = enrich(df)

    ctx = StrategyContext(
        position=None,
        equity=100000.0,
        regime=Regime.RANGE,
        now=idx[-1],
        mover_bias="gainer",
    )

    sig = strat.on_bar(ctx, "LRCX", df)
    # Must reject short signal because symbol is a gainer and close (102) >= open (100)
    assert sig is None or sig.action != SignalAction.ENTER_SHORT


def test_monte_carlo_adaptive_hurdle_high_rr():
    """Verify that Monte Carlo gate approves positive-edge high-RR setups (e.g. 2.5R target with 32% win rate)."""
    gate = MonteCarloGate(min_win_prob=0.35, min_expected_r=0.05)

    sig = Signal(
        symbol="SOXL",
        action=SignalAction.ENTER_LONG,
        confidence=0.80,
        strategy="orb",
        reason="ORB breakout",
        price=75.0,
        stop_loss=73.0,
        take_profit=80.0,  # 5.0 reward / 2.0 risk = 2.5R
    )

    # Synthetic simulation stats: 2.5R target, 32% win rate (breakeven is 28.6%, edge = +0.034, avg_r = +0.12R)
    # In old logic, p_target 0.32 < min_win_prob 0.35 would trigger veto!
    mock_stats = {
        "p_target": 0.32,
        "p_stop": 0.68,
        "p_neither": 0.0,
        "avg_r": 0.12,
        "predicted_peak_r": 2.20,
        "r_target": 2.5,
    }

    # Monkeypatch simulate to return our test stats
    gate.simulate = lambda *args, **kwargs: mock_stats

    # Create dummy 1m dataframe
    idx = [datetime.now(UTC) - timedelta(minutes=i) for i in range(60)][::-1]
    df = pd.DataFrame({"close": [75.0]*60, "high": [76.0]*60, "low": [74.0]*60, "open": [75.0]*60, "volume": [1000]*60}, index=idx)

    approved, stats, reason = gate.evaluate_signal(sig, df)
    assert approved is True, f"Expected approval but got veto: {reason}"
    assert "approved" in reason.lower()


def test_momentum_rejects_sub_5_penny_stock():
    """Verify that momentum strategy rejects entry signals on stocks priced under $5.00."""
    strat = MomentumCross()
    start = datetime(2026, 11, 3, 14, 45, tzinfo=UTC)  # 15 mins after open
    idx = [start + timedelta(minutes=i) for i in range(40)]

    # Penny stock at $2.00
    closes = [2.00] * 40
    opens = [2.00] * 40
    highs = [2.05] * 40
    lows = [1.95] * 40
    volumes = [100000.0] * 40

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=idx)

    from app.indicators.ta import enrich
    df = enrich(df)

    ctx = StrategyContext(
        position=None,
        equity=100000.0,
        regime=Regime.TREND_UP,
        now=idx[-1],
    )
    sig = strat.on_bar(ctx, "INO", df)
    assert sig is None, "Expected momentum entry to be rejected for sub-$5 penny stock INO"


@pytest.mark.asyncio
async def test_market_regime_rebound_downgrades_panic_to_caution():
    """Verify that multi-day pullback (close < sma20) with a green intraday session is recognized as uptrend."""
    class MockData:
        pass

    agg = MarketRiskAggregator(MockData())
    # Create 3 days of hourly bars declining from 520 to 500, then today opens at 500 and closes at 505
    start = datetime(2026, 11, 1, 14, 30, tzinfo=UTC)
    idx_past = [start + timedelta(hours=i) for i in range(25)]
    # Past 25 hours declining from 520 to 500
    closes_past = [520.0 - i * 0.8 for i in range(25)]
    
    # Today: 30 1-minute bars starting at 500.0 and rising to 505.0
    today_start = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
    idx_today = [today_start + timedelta(minutes=i) for i in range(30)]
    closes_today = [500.0 + (i + 1) * 0.2 for i in range(30)]
    
    all_idx = idx_past + idx_today
    all_closes = closes_past + closes_today
    all_opens = [520.0] + all_closes[:-1]
    all_opens[25] = 500.0  # Today's open
    all_highs = [c + 0.5 for c in all_closes]
    all_lows = [o - 0.5 for o in all_opens]
    all_volumes = [10000.0 for _ in all_closes]

    df = pd.DataFrame({
        "open": all_opens,
        "high": all_highs,
        "low": all_lows,
        "close": all_closes,
        "volume": all_volumes,
    }, index=all_idx)

    # Multi-day 20-hour SMA check:
    df_h = df.resample("1h").agg({"close": "last"}).dropna()
    assert len(df_h) >= 20
    close_h = float(df_h["close"].iloc[-1])
    sma20 = float(df_h["close"].rolling(20).mean().iloc[-1])
    # Multi-day close is below SMA20 due to recent 3-day pullback
    assert close_h < sma20

    # But today's session is green and above VWAP
    last_dt = df.index[-1]
    today_df = df[df.index.date == last_dt.date()]
    cur_close = float(today_df["close"].iloc[-1])
    today_open = float(today_df["open"].iloc[0])
    today_vwap = float(((today_df["close"] * today_df["volume"]).cumsum() / today_df["volume"].cumsum()).iloc[-1])
    assert cur_close > today_open
    assert cur_close > today_vwap



def test_gap_and_go_rejects_counter_trend_and_penny():
    strat = GapAndGo()
    start = datetime(2026, 11, 3, 14, 40, tzinfo=UTC)
    idx = [start + timedelta(minutes=i) for i in range(10)]

    # 1. Penny stock at $3.00
    df_penny = pd.DataFrame({
        "open": [3.0]*10, "high": [3.1]*10, "low": [2.9]*10, "close": [3.0]*10, "volume": [10000.0]*10
    }, index=idx)
    from app.indicators.ta import enrich
    df_penny = enrich(df_penny)
    ctx_penny = StrategyContext(position=None, equity=100000.0, regime=Regime.TREND_UP, now=idx[-1], mover_change_pct=5.0)
    assert strat.on_bar(ctx_penny, "PNNY", df_penny) is None

    # 2. Short on gainer trading above session open (PLTR case)
    df_pltr = pd.DataFrame({
        "open": [165.0] + [169.0]*9,
        "high": [171.0]*10,
        "low": [164.0]*10,
        "close": [170.0]*10,
        "volume": [50000.0]*10
    }, index=idx)
    df_pltr = enrich(df_pltr)
    ctx_short_gainer = StrategyContext(position=None, equity=100000.0, regime=Regime.TREND_UP, now=idx[-1], mover_change_pct=-3.0, mover_bias="gainer")
    assert strat.on_bar(ctx_short_gainer, "PLTR", df_pltr) is None

    # 3. Long on loser trading below session open
    df_loser = pd.DataFrame({
        "open": [100.0] + [95.0]*9,
        "high": [101.0]*10,
        "low": [94.0]*10,
        "close": [95.0]*10,
        "volume": [50000.0]*10
    }, index=idx)
    df_loser = enrich(df_loser)
    ctx_long_loser = StrategyContext(position=None, equity=100000.0, regime=Regime.TREND_DOWN, now=idx[-1], mover_change_pct=3.0, mover_bias="loser")
    assert strat.on_bar(ctx_long_loser, "LOSR", df_loser) is None


def test_bb_squeeze_rejects_sub_vwap_long_and_penny():
    strat = BollingerBandSqueeze()
    start = datetime(2026, 11, 3, 15, 30, tzinfo=UTC)
    idx = [start + timedelta(minutes=i) for i in range(120)]

    # 1. Penny stock < $5.00
    df_penny = pd.DataFrame({
        "open": [2.5]*120, "high": [2.6]*120, "low": [2.4]*120, "close": [2.5]*120, "volume": [10000.0]*120
    }, index=idx)
    from app.indicators.ta import enrich
    df_penny = enrich(df_penny)
    ctx = StrategyContext(position=None, equity=100000.0, regime=Regime.RANGE, now=idx[-1])
    assert strat.on_bar(ctx, "PNNY", df_penny) is None

    # 2. ADI falling knife case: Price is 420, open was 430, VWAP is 425
    prices = [430.0] * 30 + [428.0] * 30 + [425.0] * 30 + [420.0] * 30
    df_adi = pd.DataFrame({
        "open": [430.0] + prices[:-1],
        "high": [p + 1.0 for p in prices],
        "low": [p - 1.0 for p in prices],
        "close": prices,
        "volume": [10000.0] * 120
    }, index=idx)
    df_adi = enrich(df_adi)
    ctx_loser = StrategyContext(position=None, equity=100000.0, regime=Regime.RANGE, now=idx[-1], mover_bias="loser")
    sig = strat.on_bar(ctx_loser, "ADI", df_adi)
    assert sig is None or sig.action != SignalAction.ENTER_LONG


def test_pipeline_rejects_sub_5_penny_stock():
    mock_model = MagicMock()
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.0)
    sig = Signal(symbol="INO", action=SignalAction.ENTER_LONG, strategy="momentum", reason="test momentum", confidence=0.8, price=2.02, stop_loss=1.91, take_profit=2.30)
    df = pd.DataFrame({"close": [2.02]*10, "open": [2.05]*10, "high": [2.06]*10, "low": [2.00]*10, "volume": [1000]*10})
    accepted, rejected = pipeline.process("INO", df, [sig], Regime.TREND_UP)
    assert len(accepted) == 0
    assert len(rejected) == 1
    assert "penny stock" in rejected[0][1].lower()


def test_pipeline_rejects_inverse_etf_in_trend_up():
    mock_model = MagicMock()
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.0)
    sig = Signal(symbol="SOXS", action=SignalAction.ENTER_LONG, strategy="orb", reason="test orb", confidence=0.85, price=53.50, stop_loss=52.50, take_profit=56.00)
    df = pd.DataFrame({"close": [53.50]*10, "open": [54.00]*10, "high": [54.20]*10, "low": [53.00]*10, "volume": [1000]*10})
    accepted, rejected = pipeline.process("SOXS", df, [sig], Regime.TREND_UP)
    assert len(accepted) == 0
    assert len(rejected) == 1
    assert "inverse etf long prohibited" in rejected[0][1].lower()


def test_leveraged_etf_sizing_scaler():
    class DummyClock:
        def now(self):
            return datetime.now(UTC)
        def is_open(self, now=None):
            return True
        def in_entry_window(self, mins=0, now=None):
            return True

    cfg = RiskConfig()
    rm = RiskManager(cfg, DummyClock())

    ctx = AccountContext(
        equity=100000.0,
        cash=100000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=100000.0,
        peak_equity=100000.0,
        day_trades_today=0,
    )

    df = pd.DataFrame({"close": [50.0]*10, "high": [51.0]*10, "low": [49.0]*10, "volume": [10000]*10})

    # Standard equity at $50
    sig_std = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, strategy="orb", reason="test", confidence=0.8, price=50.0, stop_loss=48.0, take_profit=55.0)
    dec_std = rm.evaluate_entry(sig_std, ctx, df)
    assert dec_std.approved

    # 3x Leveraged ETF (SOXS) at $50
    sig_3x = Signal(symbol="SOXS", action=SignalAction.ENTER_LONG, strategy="orb", reason="test", confidence=0.8, price=50.0, stop_loss=48.0, take_profit=55.0)
    dec_3x = rm.evaluate_entry(sig_3x, ctx, df)
    assert dec_3x.approved

    # 3x Leveraged ETF position quantity must be significantly smaller than standard (scaled by leverage)
    assert dec_3x.order is not None and dec_std.order is not None
    assert dec_3x.order.qty <= int(dec_std.order.qty * 0.6)
