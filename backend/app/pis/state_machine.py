from __future__ import annotations

from .models import LifecycleState, ThesisState


def next_lifecycle_state(
    current: LifecycleState,
    *,
    current_r: float,
    peak_r: float,
    health: float | None,
    thesis_state: ThesisState,
) -> LifecycleState:
    """PIS v1 lifecycle classifier.

    State is deliberately conservative: runner promotion requires both profit
    progress and healthy evidence; deterioration is evidence-driven rather than
    triggered by a fixed take-profit level.
    """
    if current is LifecycleState.CLOSED:
        return current
    if health is None or thesis_state is ThesisState.UNKNOWN:
        return LifecycleState.NEW if current is LifecycleState.NEW else current
    if thesis_state is ThesisState.BROKEN or health <= 35.0:
        return LifecycleState.EXIT_CANDIDATE
    if health < 50.0 or thesis_state is ThesisState.WEAKENING:
        return LifecycleState.WEAKENING
    if peak_r >= 2.0 and health >= 75.0:
        return LifecycleState.RUNNER
    if current_r >= 1.0 and thesis_state is ThesisState.STRENGTHENING:
        return LifecycleState.ACCELERATING
    if health >= 65.0:
        return LifecycleState.VALIDATED
    return LifecycleState.DISCOVERY
