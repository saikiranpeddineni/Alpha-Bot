from datetime import datetime, timezone

from app.pis.engine_adapter import PISEvaluation
from app.pis.execution import ExecutionIntent
from app.pis.models import ExitAction, LifecycleState, PISDecision, PositionSnapshot, ThesisState
from app.pis.telemetry import evaluation_payload


def test_pis_decision_payload_contains_forensic_fields():
    snapshot = PositionSnapshot(
        timestamp=datetime.now(timezone.utc), symbol="AAA", side="long",
        lifecycle=LifecycleState.RUNNER, current_r=4.0, peak_r=5.0,
        trough_r=-0.2, giveback_r=1.0, health=82.0, entry_health=70.0,
        thesis_drift=12.0, thesis_state=ThesisState.STABLE, components={"trend": 85.0},
    )
    decision = PISDecision(ExitAction.HOLD, 88.0, ("PIS_HEALTHY_PULLBACK",), snapshot, "pis-v3")
    evaluation = PISEvaluation(decision, ExecutionIntent(False, None, True, "PIS_HOLD"))
    payload = evaluation_payload(evaluation)
    assert payload["action"] == "HOLD"
    assert payload["current_r"] == 4.0
    assert payload["peak_r"] == 5.0
    assert payload["giveback_r"] == 1.0
    assert payload["suppress_legacy_exit"] is True
    assert payload["reason_codes"] == ["PIS_HEALTHY_PULLBACK"]
