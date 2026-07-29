from datetime import datetime, timezone

from app.pis.models import LifecycleState, PositionSnapshot, ThesisState
from app.pis.protection import AdaptiveProtectionPlanner


def snap(*, current_r, peak_r, health, thesis):
    return PositionSnapshot(
        timestamp=datetime.now(timezone.utc),
        symbol="AAA",
        side="long",
        lifecycle=LifecycleState.RUNNER,
        current_r=current_r,
        peak_r=peak_r,
        trough_r=-0.2,
        giveback_r=max(0.0, peak_r - current_r),
        health=health,
        entry_health=70.0,
        thesis_drift=health - 70.0,
        thesis_state=thesis,
        components={},
    )


def test_strengthening_runner_gets_more_room_than_weakening_runner():
    planner = AdaptiveProtectionPlanner()
    strong = planner.plan(snap(current_r=4.7, peak_r=5.0, health=88, thesis=ThesisState.STRENGTHENING))
    weak = planner.plan(snap(current_r=4.7, peak_r=5.0, health=48, thesis=ThesisState.WEAKENING))
    assert strong.giveback_allowance_r > weak.giveback_allowance_r
    assert strong.floor_r < weak.floor_r


def test_two_r_does_not_create_two_r_take_profit_cap():
    plan = AdaptiveProtectionPlanner().plan(
        snap(current_r=2.0, peak_r=2.0, health=90, thesis=ThesisState.STRENGTHENING)
    )
    assert plan.floor_r is not None
    assert plan.floor_r < 2.0


def test_large_strengthening_winner_keeps_convex_breathing_room():
    plan = AdaptiveProtectionPlanner().plan(
        snap(current_r=8.0, peak_r=8.0, health=92, thesis=ThesisState.STRENGTHENING)
    )
    assert plan.giveback_allowance_r >= 0.8
    assert plan.floor_r < 8.0
    assert plan.floor_r > 0.0


def test_sub_one_r_trade_is_not_forced_to_breakeven_by_pis():
    plan = AdaptiveProtectionPlanner().plan(
        snap(current_r=0.9, peak_r=0.9, health=82, thesis=ThesisState.STRENGTHENING)
    )
    assert plan.floor_r is not None
    assert plan.floor_r <= 0.0
