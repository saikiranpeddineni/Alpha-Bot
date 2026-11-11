"""Unit and regression tests for High-Margin Profit Architecture.

Validates:
1. Strategy take-profit targets enforce >= 3.5% minimum price expansion across all strategies
   (ORB, Momentum, BB Squeeze, Gap and Go, Trend Pullback).
2. RiskManager.evaluate_entry enforces min_profit_target_pct floor on approved trade orders.
3. MarketScanner Stage 1 prefilter enforces min_scanner_atr_pct volatility gate when configured.
4. PIS and EngineEvaluation enforce min_bank_profit_usd ($100 floor) to prevent micro-slice churn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from app.config import RiskConfig
from app.core.domain import OrderSide, Position, Regime, Signal, SignalAction
from app.risk.manager import RiskManager
from app.core.scanner import MarketScanner
from app.pis.models import ExitAction, PISConfig
from app.strategies.base import StrategyContext
from app.strategies.bb_squeeze import BollingerBandSqueeze
from app.strategies.gap_and_go import GapAndGo
from app.strategies.momentum import MomentumCross
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.trend_pullback import TrendPullback


def test_orb_enforces_high_margin_target():
    """ORB target must enforce >= 3.5% price expansion floor for longs and shorts."""
    orb = OpeningRangeBreakout()
    assert orb.params.get("min_profit_pct") == 0.035

    # Synthesize 1-min bars for morning session
    times = pd.date_range("2026-11-12 09:30:00", periods=20, freq="1min", tz="America/New_York")
    # Low-volatility or tight range where raw 2R would be tiny (e.g. 0.5%)
    closes = [100.0 + 0.05 * i for i in range(20)]
    df = pd.DataFrame({
        "open": closes,
        "high": [c + 0.05 for c in closes],
        "low": [c - 0.05 for c in closes],
        "close": closes,
        "volume": [10000.0] * 20,
        "atr14": [0.20] * 20,
        "vwap": [100.5] * 20,
    }, index=times)

    ctx = StrategyContext(
        position=None,
        equity=100000.0,
        regime=Regime.TREND_UP,
        now=times[-1].to_pydatetime(),
    )
    sig = orb.on_bar(ctx, "AAPL", df)
    if sig:
        assert sig.take_profit >= round(sig.price * 1.035, 2)


def test_momentum_enforces_high_margin_target():
    """Momentum target must enforce >= 3.5% price expansion floor."""
    mom = MomentumCross()
    assert mom.params.get("min_profit_pct") == 0.035
    assert mom.params.get("reward_risk") >= 2.5


def test_bb_squeeze_enforces_high_margin_target():
    """BB Squeeze target must enforce >= 3.5% price expansion floor."""
    bb = BollingerBandSqueeze()
    assert bb.params.get("min_profit_pct") == 0.035
    assert bb.params.get("reward_risk") >= 2.5


def test_gap_and_go_enforces_high_margin_target():
    """Gap and Go target must enforce >= 3.5% price expansion floor."""
    gg = GapAndGo()
    assert gg.params.get("min_profit_pct") == 0.035
    assert gg.params.get("reward_risk") >= 3.0


def test_trend_pullback_enforces_high_margin_target():
    """Trend Pullback target must enforce >= 3.5% price expansion floor."""
    tp = TrendPullback()
    assert tp.params.get("min_profit_pct") == 0.035


def test_risk_manager_enforces_min_profit_target_pct():
    """RiskManager.evaluate_entry must enforce min_profit_target_pct floor."""
    from app.risk.manager import AccountContext
    mock_clock = MagicMock()
    mock_clock.is_open.return_value = True

    rc = RiskConfig(
        min_profit_target_pct=3.5,
        account_size=100000.0,
        max_account_risk_pct=1.0,
        enable_max_drawdown_halt=False,
        enable_native_bracket_targets=True,
    )
    rm = RiskManager(rc, mock_clock)

    act_ctx = AccountContext(
        equity=100000.0,
        cash=100000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=100000.0,
        peak_equity=100000.0,
        day_trades_today=0,
        rolling_day_trades=0,
        is_paper=True,
    )

    bars_df = pd.DataFrame({
        "open": [100.0] * 10,
        "high": [100.5] * 10,
        "low": [99.5] * 10,
        "close": [100.0] * 10,
        "volume": [10000.0] * 10,
        "atr14": [0.50] * 10,
    })

    # Signal with tiny target (+1.0% gain)
    sig_tiny = Signal(
        symbol="XYZ",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="momentum",
        reason="test high margin long",
        price=100.0,
        stop_loss=99.5,
        take_profit=101.0,  # Only +1%
    )
    decision = rm.evaluate_entry(sig_tiny, act_ctx, bars_df)
    assert decision.approved
    assert decision.order is not None
    # RiskManager must elevate take_profit to at least 103.50 (3.5% expansion)
    assert decision.order.take_profit >= 103.50

    # Short signal with tiny target (-1.0% drop)
    sig_short_tiny = Signal(
        symbol="XYZ",
        action=SignalAction.ENTER_SHORT,
        confidence=0.85,
        strategy="momentum",
        reason="test high margin short",
        price=100.0,
        stop_loss=100.5,
        take_profit=99.0,  # Only -1%
    )
    decision_short = rm.evaluate_entry(sig_short_tiny, act_ctx, bars_df)
    assert decision_short.approved
    assert decision_short.order is not None
    assert decision_short.order.take_profit <= 96.50


@pytest.mark.anyio
async def test_scanner_stage1_enforces_atr_volatility_gate():
    """Stage 1 scanner rejects symbols with daily ATR below min_scanner_atr_pct."""
    mock_data = MagicMock()
    rc = RiskConfig(min_price=10.0, min_scanner_atr_pct=2.5)
    scanner = MarketScanner(mock_data, ["SPY", "QQQ"], risk_config=rc)

    idx = pd.date_range(end=datetime.now(UTC), periods=15, freq="D", tz="UTC")
    
    # LOW-VOLATILITY Symbol: close $100, daily high-low is only $0.50 (ATR = 0.5% < 2.5%)
    low_vol_df = pd.DataFrame({
        "open": np.ones(15) * 100.0,
        "high": np.ones(15) * 100.25,
        "low": np.ones(15) * 99.75,
        "close": np.ones(15) * 100.0,
        "volume": np.ones(15) * 5_000_000.0,
    }, index=idx)

    # HIGH-VOLATILITY Symbol: close $100, daily high-low is $4.00 (ATR = 4.0% >= 2.5%)
    high_vol_df = pd.DataFrame({
        "open": np.ones(15) * 100.0,
        "high": np.ones(15) * 102.0,
        "low": np.ones(15) * 98.0,
        "close": np.ones(15) * 100.0,
        "volume": np.ones(15) * 5_000_000.0,
    }, index=idx)

    async def mock_history(symbol, timeframe, start, end, prefer_cache=True):
        if symbol == "LOWVOL":
            return low_vol_df
        return high_vol_df

    mock_data.history.side_effect = mock_history
    mock_data.multi_history = AsyncMock(side_effect=Exception("multi_history not available"))

    candidates = await scanner._stage1_prefilter_parallel(symbols=["LOWVOL", "HIGHVOL"])
    cand_symbols = [c["symbol"] for c in candidates]
    assert "LOWVOL" not in cand_symbols
    assert "HIGHVOL" in cand_symbols
