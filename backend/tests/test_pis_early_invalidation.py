"""Unit and Integration Tests for PIS Early Invalidation and Asymmetric Loss Reduction (Quadrant 2)."""

from datetime import datetime, timezone

import pytest

from app.pis.models import ExitAction, PISConfig, PositionObservation, ReasonCode
from app.pis.position_brain import PositionBrain


def _make_obs(price: float, high: float, low: float, vol: float = 1000.0, r_mult: float = 0.0):
    return PositionObservation(
        timestamp=datetime.now(timezone.utc),
        symbol="FAILTEST",
        side=1.0,
        qty=100,
        entry_price=100.0,
        current_price=price,
        high=high,
        low=low,
        close=price,
        volume=vol,
        atr=1.0,
        initial_stop=98.0,
        risk_per_share=2.0,
        r_multiple=r_mult,
    )


def test_early_invalidation_requires_confirmed_adverse_bars():
    """A single adverse candle is noise; confirmed failure may still scratch early."""
    initial_obs = _make_obs(100.0, 100.2, 99.8, vol=1000, r_mult=0.0)
    brain = PositionBrain("FAILTEST", initial_obs)

    # First adverse bar: do not panic-exit on one print.
    bar2_obs = _make_obs(98.7, 99.8, 98.6, vol=5000, r_mult=-0.65)
    decision_bar2 = brain.observe(bar2_obs)
    assert decision_bar2.action != ExitAction.FULL_EXIT
    assert decision_bar2.thesis.early_invalidation_detected is False

    # Second consecutive adverse observation: confirmed structural failure.
    decision = brain.observe(_make_obs(98.6, 99.7, 98.4, vol=5000, r_mult=-0.70))
    assert decision.action == ExitAction.FULL_EXIT
    assert ReasonCode.EARLY_THESIS_INVALIDATED in decision.reason_codes
    assert decision.thesis.early_invalidation_detected is True
    assert "ASYMMETRIC LOSS CUT" in decision.brainstorm_narrative
