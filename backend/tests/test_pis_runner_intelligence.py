from datetime import datetime, timezone

from app.pis.exit_planner import PISExitPlanner
from app.pis.models import ExitAction, LifecycleState, PositionSnapshot, ThesisState
from app.pis.profit_memory import ProfitMemory
from app.pis.runner_intelligence import RunnerIntelligence


def snapshot(*, current_r, peak_r, health, thesis, lifecycle=LifecycleState.RUNNER):
    return PositionSnapshot(
        timestamp=datetime.now(timezone.utc), symbol="AAA", side="long",
        lifecycle=lifecycle, current_r=current_r, peak_r=peak_r,
        trough_r=-0.25, giveback_r=max(0.0, peak_r-current_r), health=health,
        entry_health=70.0, thesis_drift=health-70.0, thesis_state=thesis,
        components={},
    )


def test_5r_to_4r_healthy_pullback_is_not_exit():
    snap = snapshot(current_r=4.0, peak_r=5.0, health=82, thesis=ThesisState.STABLE)
    pullback = ProfitMemory().assess(snap)
    decision = PISExitPlanner().decide(snap)
    assert pullback.healthy_pullback
    assert decision.action is ExitAction.HOLD
    assert not decision.requests_exit


def test_exceptional_runner_is_identified_without_profit_cap():
    snap = snapshot(current_r=7.5, peak_r=8.0, health=92, thesis=ThesisState.STRENGTHENING)
    assessment = RunnerIntelligence().assess(snap)
    decision = PISExitPlanner().decide(snap)
    assert assessment.exceptional_runner
    assert decision.action in {ExitAction.HOLD, ExitAction.TRAIL}
    assert not decision.requests_exit


def test_large_giveback_plus_weak_health_is_distribution_risk():
    snap = snapshot(current_r=2.4, peak_r=5.0, health=45, thesis=ThesisState.WEAKENING, lifecycle=LifecycleState.WEAKENING)
    pullback = ProfitMemory().assess(snap)
    assert pullback.distribution_risk
    decision = PISExitPlanner().decide(snap)
    assert decision.action is ExitAction.TIGHTEN_PROTECTION


def test_peak_r_alone_never_forces_exit():
    planner = PISExitPlanner()
    for peak in (1.0, 2.0, 3.0, 5.0, 8.0, 10.0):
        snap = snapshot(current_r=peak, peak_r=peak, health=90, thesis=ThesisState.STRENGTHENING)
        assert planner.decide(snap).action is not ExitAction.EXIT
