"""Tests for the intelligent profit-taking stack added after the 2026-08-24
session forensics (book peaked +$718.70, closed -$391.03):

1. Progressive profit lock: protection floor must leave the initial stop once
   peak_r >= breakeven_trigger_r (previously nothing locked below 1.5R peak).
2. Profit giveback guard: a green trade that surrendered most of its excursion
   banks a partial instead of round-tripping.
3. Trend-quality adaptive scale-out: weakening trends bank at 1R, strong trends
   keep scale_out_target_r.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.pis.models import ExitAction, PISConfig, PositionObservation, ReasonCode
from app.pis.position_brain import PositionBrain


def _obs(
    price: float,
    entry: float = 100.0,
    initial_stop: float = 95.0,
    atr: float = 2.0,
    qty: float = 100.0,
    side: float = 1.0,
    minutes_offset: int = 0,
) -> PositionObservation:
    risk = abs(entry - initial_stop)
    r_multiple = (price - entry) / risk * side
    ts = datetime(2026, 8, 25, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=minutes_offset)
    return PositionObservation(
        timestamp=ts,
        symbol="TEST",
        side=side,
        qty=qty,
        entry_price=entry,
        current_price=price,
        high=max(price, entry),
        low=min(price, entry),
        close=price,
        volume=10000.0,
        atr=atr,
        initial_stop=initial_stop,
        risk_per_share=risk,
        current_stop=None,
        take_profit=None,
        r_multiple=r_multiple,
    )


def test_progressive_lock_ratchets_floor_above_breakeven() -> None:
    """At +0.9R peak (low-noise name: atr_r=0.2) the floor is above breakeven; a
    later adverse bar cannot lower it."""
    config = PISConfig(scale_out_target_r=2.0)
    brain = PositionBrain("TEST", _obs(100.0, atr=1.0), config)
    assert brain.current_stop_floor == 95.0

    dec = brain.observe(_obs(104.5, atr=1.0, minutes_offset=1))  # +0.9R
    assert dec.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
    floor_at_peak = brain.current_stop_floor
    assert floor_at_peak >= 100.0, "floor must be at least breakeven once peak >= trigger"

    # Giveback to -0.4R: monotonicity keeps the ratcheted floor.
    brain.observe(_obs(98.0, atr=1.0, minutes_offset=2))
    assert brain.current_stop_floor == floor_at_peak


def test_noisy_name_does_not_arm_lock_on_sub_noise_peak() -> None:
    """CRWV-style geometry: ATR is a large fraction of the risk unit, so an
    early 0.77R peak is inside the churn band and must NOT ratchet the floor
    above the initial stop."""
    config = PISConfig(scale_out_target_r=2.0)
    # risk = 5.0, atr = 8.0 -> atr_r = 1.6 -> trigger = max(0.40, 4.0) = 4.0R
    brain = PositionBrain("TEST", _obs(100.0, initial_stop=95.0, atr=8.0), config)
    brain.observe(_obs(102.0, initial_stop=95.0, atr=8.0, minutes_offset=1))  # +0.4R
    brain.observe(_obs(103.85, initial_stop=95.0, atr=8.0, minutes_offset=2))  # +0.77R
    assert brain.current_stop_floor == 95.0, "sub-noise peak must not move the floor"


def test_below_breakeven_trigger_keeps_initial_geometry() -> None:
    """Small excursions (< 0.40R) must not be shaken out by a premature floor."""
    config = PISConfig(breakeven_trigger_r=0.40)
    brain = PositionBrain("TEST", _obs(100.0), config)
    brain.observe(_obs(101.5, minutes_offset=1))  # +0.3R < trigger
    assert brain.current_stop_floor <= 95.0


def test_giveback_guard_banks_partial_while_green() -> None:
    """Peak +1.0R then fall back to +0.35R -> PARTIAL_EXIT with guard reason."""
    config = PISConfig(scale_out_target_r=2.0)
    brain = PositionBrain("TEST", _obs(100.0), config)

    brain.observe(_obs(105.0, minutes_offset=1))  # +1.0R peak
    dec = brain.observe(_obs(101.75, minutes_offset=2))  # +0.35R, still green

    assert dec.action == ExitAction.PARTIAL_EXIT
    assert ReasonCode.PROFIT_GIVEBACK_GUARD in dec.reason_codes
    assert brain.has_scaled_out is True


def test_giveback_guard_ignores_new_highs_and_red_trades() -> None:
    config = PISConfig(scale_out_target_r=2.0)
    # Making new highs: no guard.
    brain = PositionBrain("TEST", _obs(100.0), config)
    dec = brain.observe(_obs(105.0, minutes_offset=1))
    assert dec.action != ExitAction.PARTIAL_EXIT or ReasonCode.SCALE_OUT_TARGET in dec.reason_codes

    # Deep red after a peak: guard must NOT fire (protection floor owns losses).
    brain2 = PositionBrain("TEST", _obs(100.0), config)
    brain2.observe(_obs(105.0, minutes_offset=1))
    dec2 = brain2.observe(_obs(98.5, minutes_offset=2))  # -0.3R
    if dec2.action == ExitAction.PARTIAL_EXIT:
        assert ReasonCode.PROFIT_GIVEBACK_GUARD not in dec2.reason_codes

def test_weak_trend_banks_scale_out_at_1r() -> None:
    """With broken momentum, scale-out drops from 2R to weak_trend_scale_out_r."""
    config = PISConfig(
        enable_profit_giveback_guard=False,
        weak_trend_scale_out_r=1.0,
        weak_momentum_threshold=0.90,  # force 'weakening' classification
    )
    brain = PositionBrain("TEST", _obs(100.0), config)
    dec = brain.observe(_obs(105.0, minutes_offset=1))  # +1.0R

    assert dec.action == ExitAction.PARTIAL_EXIT
    assert ReasonCode.SCALE_OUT_TARGET in dec.reason_codes
    assert brain.has_scaled_out is True
