from datetime import datetime, timezone

from app.pis import ExitAction, LifecycleState, PositionObservation, PositionBrain
from app.pis.service import PositionIntelligenceSystem


def obs(price, *, trend=80, momentum=80, volume=80, rs=80, market=80):
    return PositionObservation(
        timestamp=datetime.now(timezone.utc),
        price=price,
        trend=trend,
        momentum=momentum,
        volume=volume,
        relative_strength=rs,
        market_context=market,
    )


def test_long_and_short_r_are_directionally_correct():
    now = datetime.now(timezone.utc)
    long = PositionBrain("AAA", "long", 100, 98, now)
    short = PositionBrain("BBB", "short", 100, 102, now)
    assert long.r_multiple(104) == 2.0
    assert long.r_multiple(98) == -1.0
    assert short.r_multiple(96) == 2.0
    assert short.r_multiple(102) == -1.0


def test_fixed_two_r_is_not_an_exit_condition_for_healthy_runner():
    now = datetime.now(timezone.utc)
    brain = PositionBrain("AAA", "long", 100, 98, now)
    brain.observe(obs(100, trend=75, momentum=75, volume=75, rs=75, market=75))
    decision = brain.observe(obs(104, trend=92, momentum=92, volume=90, rs=91, market=88))
    assert decision.snapshot.current_r == 2.0
    assert decision.snapshot.lifecycle is LifecycleState.RUNNER
    assert decision.action is ExitAction.TRAIL
    assert not decision.requests_exit


def test_broken_thesis_is_pis_exit():
    now = datetime.now(timezone.utc)
    brain = PositionBrain("AAA", "long", 100, 98, now)
    brain.observe(obs(100, trend=85, momentum=85, volume=85, rs=85, market=85))
    decision = brain.observe(obs(99, trend=20, momentum=20, volume=30, rs=20, market=30))
    assert decision.snapshot.lifecycle is LifecycleState.EXIT_CANDIDATE
    assert decision.action is ExitAction.EXIT
    assert decision.requests_exit


def test_position_intelligence_system_owns_position_brains():
    now = datetime.now(timezone.utc)
    pis = PositionIntelligenceSystem()
    pis.register(symbol="aaa", side="long", entry_price=100, initial_stop=98, entry_time=now)
    assert pis.get("AAA") is not None
    decision = pis.evaluate("AAA", obs(100))
    assert decision.action in {ExitAction.KEEP_PLAN, ExitAction.HOLD}
    pis.close("AAA")
    assert pis.get("AAA") is None
