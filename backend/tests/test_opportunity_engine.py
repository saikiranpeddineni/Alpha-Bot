from __future__ import annotations

import pandas as pd

from app.config import Settings
from app.core.domain import Signal, SignalAction
from app.opportunities.engine import OpportunityEngine
from app.opportunities.models import OpportunityContext, OpportunitySession


def _signal(**updates):
    values = dict(
        symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=0.8,
        strategy="test", reason="test", price=100.0,
        stop_loss=98.0, take_profit=104.0,
    )
    values.update(updates)
    return Signal(**values)


def test_candidate_is_cost_aware_and_keeps_session_intent():
    candidate = OpportunityEngine().evaluate(
        _signal(), pd.DataFrame({"close": [100.0]}),
        OpportunityContext(
            session=OpportunitySession.OVERNIGHT,
            estimated_round_trip_cost_bps=10.0,
            min_net_edge_bps=100.0,
            min_reward_risk=1.5,
        ),
    )
    assert candidate is not None
    assert candidate.gross_edge_bps == 400.0
    assert candidate.net_edge_bps == 390.0
    assert candidate.session == OpportunitySession.OVERNIGHT


def test_cost_dominated_or_bad_geometry_is_rejected():
    engine = OpportunityEngine()
    frame = pd.DataFrame({"close": [100.0]})
    assert engine.evaluate(
        _signal(take_profit=100.1), frame,
        OpportunityContext(min_net_edge_bps=20.0),
    ) is None
    assert engine.evaluate(
        _signal(stop_loss=100.0), frame, OpportunityContext()
    ) is None


def test_engine_does_not_evaluate_exits_as_new_opportunities():
    assert OpportunityEngine().evaluate(
        _signal(action=SignalAction.EXIT), pd.DataFrame({"close": [100.0]}), OpportunityContext()
    ) is None


def test_opportunity_thresholds_are_configurable_and_overnight_is_off_by_default(monkeypatch):
    monkeypatch.setenv("OPPORTUNITY_MIN_NET_EDGE_BPS", "25")
    monkeypatch.setenv("OPPORTUNITY_MIN_REWARD_RISK", "1.8")
    settings = Settings(_env_file=None)
    config = settings.opportunity_config()
    assert config.min_net_edge_bps == 25
    assert config.min_reward_risk == 1.8
    assert config.overnight_enabled is False
