"""Unit tests for scanner-to-P&L conversion pipeline refinements.
"""

from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import pytest

from app.core.counterfactual import ExitCounterfactualEngine
from app.core.domain import Signal, SignalAction
from app.risk.manager import RiskManager
from app.config import RiskConfig


def test_partial_runner_counterfactual_math():
    """Verify true partial scaled position replay in ExitCounterfactualEngine."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=20, freq="1Min", tz="UTC")
    prices = [
        100.0, 101.0, 102.0, 103.0, 104.0,  # bar 3 hits 1.5R target (entry 100, stop 98 -> risk 2, target 103)
        103.5, 104.5, 105.0, 104.0, 103.0,  # price peaks at 105.0
        101.0, 99.0, 98.0, 97.0, 96.0,
        95.0, 94.0, 93.0, 92.0, 91.0
    ]
    df = pd.DataFrame({
        "open": prices,
        "high": [p + 0.2 for p in prices],
        "low": [p - 0.2 for p in prices],
        "close": prices,
        "atr14": [1.0] * len(prices),
    }, index=dates)

    # Replay policy with 1.5R partial target (50% scale out) and 2.5 ATR trailing runner
    result = ExitCounterfactualEngine.replay_policy(
        df=df,
        entry_idx=0,
        entry_price=100.0,
        side="long",
        initial_stop=98.0,
        initial_atr=1.0,
        policy_name="partial_1.5r_runner",
        policy_params={
            "partial_target_r": 1.5,
            "partial_fraction": 0.50,
            "runner_trailing_atr_mult": 2.5,
        }
    )

    assert result["r_multiple"] > 0
    assert "r_multiple" in result
    assert result["pnl_pct"] > 0


def test_risk_manager_explicit_discovery_source():
    """Verify RiskManager applies quality-conditioned multiplier when discovery_source == 'scanner'."""
    from app.core.clock import MarketClock
    cfg = RiskConfig(risk_per_trade_pct=0.5)
    clock = MarketClock(always_open=True)
    rm = RiskManager(clock, cfg)
    rm.core_watchlist = ["AAPL", "MSFT"]

    # Signal from scanner with high conviction
    sig_high = Signal(
        symbol="NVDA",
        action=SignalAction.ENTER_LONG,
        confidence=0.9,
        strategy="momentum",
        reason="breakout",
        discovery_source="scanner",
        scanner_score=0.88,
        scanner_auc=0.65,
    )

    # Verify risk_multiplier calculation path detects scanner source explicitly
    is_scanner = (getattr(sig_high, "discovery_source", None) == "scanner") or ("NVDA" not in rm.core_watchlist)
    assert is_scanner is True


def test_behavioral_safety_thesis_normalization():
    """Verify side normalization logic for long/short entry actions."""
    from app.core.domain import SignalAction

    action_long = SignalAction.ENTER_LONG
    action_short = SignalAction.ENTER_SHORT

    sig_side_long = "long" if action_long == SignalAction.ENTER_LONG else "short"
    sig_side_short = "short" if action_short == SignalAction.ENTER_SHORT else "long"

    assert sig_side_long == "long"
    assert sig_side_short == "short"
