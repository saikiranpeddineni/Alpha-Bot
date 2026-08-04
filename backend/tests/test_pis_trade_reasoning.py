"""Unit and Integration Tests for PIS Autonomous Trade Reasoning and Telemetry (Quadrant 4)."""

from datetime import datetime, timezone

import pytest

from app.pis.models import ExitAction, MarketRegime, PositionObservation
from app.pis.position_brain import PositionBrain


def _make_obs(symbol: str, price: float, high: float, low: float, vol: float = 1000.0, r_mult: float = 0.0):
    return PositionObservation(
        timestamp=datetime.now(timezone.utc),
        symbol=symbol,
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


def test_trade_reasoning_narrative_generated():
    """Verify that PositionBrain outputs explainable, rich brainstorming narrative telemetry."""
    obs1 = _make_obs("CRDO", 100.0, 100.5, 99.5, vol=1000, r_mult=0.0)
    brain = PositionBrain("CRDO", obs1)

    obs2 = _make_obs("CRDO", 102.0, 102.5, 100.2, vol=3000, r_mult=1.0)
    decision = brain.evaluate(obs2)

    assert decision.brainstorm_narrative != ""
    assert "CRDO LONG" in decision.brainstorm_narrative
    assert "Regime:" in decision.brainstorm_narrative
    assert "P&L:" in decision.brainstorm_narrative
    assert "TARGET CAPACITY:" in decision.brainstorm_narrative or "SCALE-OUT EXECUTING" in decision.brainstorm_narrative
    assert decision.market_regime is not None
