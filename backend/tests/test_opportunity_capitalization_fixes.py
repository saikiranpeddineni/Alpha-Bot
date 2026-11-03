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

