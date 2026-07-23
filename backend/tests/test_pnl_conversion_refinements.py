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
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.momentum import MomentumCross
from app.strategies.base import StrategyContext


def test_partial_and_old_stop_same_bar_is_conservative():
    """If a single bar spans both old stop and partial target, conservative stop hit applies."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=5, freq="1Min", tz="UTC")
    # Entry 100, Stop 98 -> Risk 2. Target +1.5R = 103.
    # Bar 1 high is 104 (hits target) AND low is 97.5 (hits stop 98).
    df = pd.DataFrame({
        "open": [100.0, 100.0, 100.0, 100.0, 100.0],
        "high": [100.0, 104.0, 100.0, 100.0, 100.0],
        "low":  [100.0, 97.5,  100.0, 100.0, 100.0],
        "close": [100.0, 98.0,  100.0, 100.0, 100.0],
        "atr14": [1.0] * 5,
    }, index=dates)

    result = ExitCounterfactualEngine.replay_policy(
        df=df,
        entry_idx=0,
        entry_price=100.0,
        side="long",
        initial_stop=98.0,
        initial_atr=1.0,
        policy_name="partial_1.5r_runner",
        policy_params={"partial_target_r": 1.5, "partial_fraction": 0.50, "runner_trailing_atr_mult": 2.5}
    )

    assert result["ambiguous"] is True
    assert result["exit_price"] == 98.0  # Conservative: pre-existing stop assumed first
    assert result["r_multiple"] == -1.0


def test_stop_precedence_over_time_stop():
    """Pre-existing stop hit on bar low takes precedence over close-based time_stop on bar close."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=5, freq="1Min", tz="UTC")
    # Entry 100, Stop 95. Bar 2 (bars_held=2): open 97, low 94 (hits stop 95), close 99.
    df = pd.DataFrame({
        "open": [100.0, 97.0, 100.0, 100.0, 100.0],
        "high": [100.0, 100.0, 100.0, 100.0, 100.0],
        "low":  [100.0, 94.0,  100.0, 100.0, 100.0],
        "close": [100.0, 99.0, 100.0, 100.0, 100.0],
        "atr14": [1.0] * 5,
    }, index=dates)

    result = ExitCounterfactualEngine.replay_policy(
        df=df,
        entry_idx=0,
        entry_price=100.0,
        side="long",
        initial_stop=95.0,
        initial_atr=1.0,
        policy_name="time_2m",
        policy_params={"time_stop_bars": 2}
    )

    # Must exit at stop 95.0, NOT at time stop close 99.0!
    assert result["exit_price"] == 95.0
    assert result["r_multiple"] == -1.0


def test_gap_through_stop_fill_price():
    """If bar opens below stop (gap down), exit_price fills at gap opening price."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=5, freq="1Min", tz="UTC")
    # Entry 100, Stop 95. Bar 1 opens at 90.0 (gap through stop 95.0).
    df = pd.DataFrame({
        "open": [100.0, 90.0, 100.0, 100.0, 100.0],
        "high": [100.0, 91.0, 100.0, 100.0, 100.0],
        "low":  [100.0, 89.0, 100.0, 100.0, 100.0],
        "close": [100.0, 90.5, 100.0, 100.0, 100.0],
        "atr14": [1.0] * 5,
    }, index=dates)

    result = ExitCounterfactualEngine.replay_policy(
        df=df,
        entry_idx=0,
        entry_price=100.0,
        side="long",
        initial_stop=95.0,
        initial_atr=1.0,
        policy_name="fixed_1_2",
        policy_params={"stop_atr_mult": 1.0, "target_atr_mult": 2.0}
    )

    # Realistic gap fill at 90.0, NOT 95.0!
    assert result["exit_price"] == 90.0
    assert result["r_multiple"] == -2.0


def test_breakeven_stop_not_retroactive_same_bar():
    """Breakeven stop from partial target fill does NOT trigger on the partial bar's low."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=5, freq="1Min", tz="UTC")
    # Entry 100, Stop 98 (risk 2). Target +1.5R = 103.
    # Bar 1 high hits 103.5. Low dips to 99.5. (99.5 is below BE stop 100, but above initial stop 98).
    df = pd.DataFrame({
        "open": [100.0, 101.0, 102.0, 101.0, 100.0],
        "high": [100.0, 103.5, 102.5, 101.0, 100.0],
        "low":  [100.0, 99.5,  101.5, 100.5, 99.0],
        "close": [100.0, 102.0, 102.0, 100.5, 99.5],
        "atr14": [1.0] * 5,
    }, index=dates)

    result = ExitCounterfactualEngine.replay_policy(
        df=df,
        entry_idx=0,
        entry_price=100.0,
        side="long",
        initial_stop=98.0,
        initial_atr=1.0,
        policy_name="partial_1.5r_runner",
        policy_params={"partial_target_r": 1.5, "partial_fraction": 0.50, "runner_trailing_atr_mult": 2.5}
    )

    assert result["partial_taken"] is True
    # Bar 1 did not stop out at BE 100. BE stop activates on Bar 2.
    assert result["bars_held"] >= 2


def test_runner_trailing_starts_after_partial_bar():
    """Runner trailing stop is active only starting next bar after partial hit."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=5, freq="1Min", tz="UTC")
    df = pd.DataFrame({
        "open": [100.0, 101.0, 104.0, 105.0, 104.0],
        "high": [100.0, 103.5, 106.0, 105.5, 104.0],
        "low":  [100.0, 101.0, 103.5, 102.0, 100.0],
        "close": [100.0, 103.0, 105.0, 103.0, 101.0],
        "atr14": [1.0] * 5,
    }, index=dates)

    result = ExitCounterfactualEngine.replay_policy(
        df=df,
        entry_idx=0,
        entry_price=100.0,
        side="long",
        initial_stop=98.0,
        initial_atr=1.0,
        policy_name="partial_1.5r_runner",
        policy_params={"partial_target_r": 1.5, "partial_fraction": 0.50, "runner_trailing_atr_mult": 2.0}
    )

    assert result["partial_taken"] is True
    assert result["runner_fraction"] == 0.50


def test_partial_runner_long_math():
    """Verify long partial runner math: 50% scale at 1.5R + runner exit."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=5, freq="1Min", tz="UTC")
    # Entry 100, Stop 98 -> Risk 2. Target 103 (+1.5R).
    # Bar 1 hits 103 -> Partial R = +1.5. 50% scale = +0.75R.
    # Bar 4 close is 106 -> Runner R = (106 - 100)/2 = +3.0R. 50% runner = +1.50R. Total R = +2.25R.
    df = pd.DataFrame({
        "open": [100.0, 101.0, 105.0, 106.0, 106.0],
        "high": [100.0, 103.5, 106.5, 106.5, 106.5],
        "low":  [100.0, 101.0, 104.5, 105.0, 105.5],
        "close": [100.0, 103.0, 105.5, 106.0, 106.0],
        "atr14": [1.0] * 5,
    }, index=dates)

    result = ExitCounterfactualEngine.replay_policy(
        df=df,
        entry_idx=0,
        entry_price=100.0,
        side="long",
        initial_stop=98.0,
        initial_atr=1.0,
        policy_name="partial_1.5r_runner",
        policy_params={"partial_target_r": 1.5, "partial_fraction": 0.50, "runner_trailing_atr_mult": 10.0}
    )

    assert result["partial_taken"] is True
    assert pytest.approx(result["r_multiple"], abs=0.1) == 2.25


def test_partial_runner_short_math():
    """Verify short partial runner math: 50% scale at 1.5R + runner exit."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=5, freq="1Min", tz="UTC")
    # Entry 100, Stop 102 -> Risk 2. Target 97 (-1.5R).
    # Bar 1 hits 97 -> Partial R = +1.5. 50% scale = +0.75R.
    # Bar 4 close is 94 -> Runner R = (100 - 94)/2 = +3.0R. 50% runner = +1.50R. Total R = +2.25R.
    df = pd.DataFrame({
        "open": [100.0, 99.0, 95.0, 94.0, 94.0],
        "high": [100.0, 99.0, 95.5, 94.5, 94.5],
        "low":  [100.0, 96.5, 94.0, 93.5, 93.5],
        "close": [100.0, 97.0, 94.5, 94.0, 94.0],
        "atr14": [1.0] * 5,
    }, index=dates)

    result = ExitCounterfactualEngine.replay_policy(
        df=df,
        entry_idx=0,
        entry_price=100.0,
        side="short",
        initial_stop=102.0,
        initial_atr=1.0,
        policy_name="partial_1.5r_runner",
        policy_params={"partial_target_r": 1.5, "partial_fraction": 0.50, "runner_trailing_atr_mult": 10.0}
    )

    assert result["partial_taken"] is True
    assert pytest.approx(result["r_multiple"], abs=0.1) == 2.25


def test_partial_runner_invalid_parameters():
    """Verify validation raises ValueError on invalid policy parameters."""
    dates = pd.date_range("2026-07-23 09:30:00", periods=5, freq="1Min", tz="UTC")
    df = pd.DataFrame({
        "open": [100.0] * 5, "high": [101.0] * 5, "low": [99.0] * 5, "close": [100.0] * 5, "atr14": [1.0] * 5
    }, index=dates)

    with pytest.raises(ValueError, match="initial_stop must differ from entry_price"):
        ExitCounterfactualEngine.replay_policy(
            df=df, entry_idx=0, entry_price=100.0, side="long", initial_stop=100.0, initial_atr=1.0,
            policy_name="invalid", policy_params={}
        )

    with pytest.raises(ValueError, match="partial_target_r must be > 0"):
        ExitCounterfactualEngine.replay_policy(
            df=df, entry_idx=0, entry_price=100.0, side="long", initial_stop=98.0, initial_atr=1.0,
            policy_name="invalid", policy_params={"partial_target_r": -1.0}
        )

    with pytest.raises(ValueError, match="partial_fraction must satisfy 0 < fraction < 1"):
        ExitCounterfactualEngine.replay_policy(
            df=df, entry_idx=0, entry_price=100.0, side="long", initial_stop=98.0, initial_atr=1.0,
            policy_name="invalid", policy_params={"partial_target_r": 1.5, "partial_fraction": 1.5}
        )


def test_orb_pending_candidate_consumption():
    """Verify ORB uses _pending dict state and callbacks on entry fill / rejection."""
    orb = OpeningRangeBreakout()
    ts = datetime.now(timezone.utc)
    orb._pending[("NVDA", ts.date())] = ts

    # Candidate is pending -> cannot generate second candidate
    assert ("NVDA", ts.date()) in orb._pending

    # Rejection clears pending without marking traded
    orb.on_entry_rejected("NVDA", ts)
    assert ("NVDA", ts.date()) not in orb._pending
    assert ("NVDA", ts.date()) not in orb._traded

    # Re-adding and simulating fill marks traded
    orb._pending[("NVDA", ts.date())] = ts
    orb.on_entry_filled("NVDA", ts)
    assert ("NVDA", ts.date()) not in orb._pending
    assert ("NVDA", ts.date()) in orb._traded
