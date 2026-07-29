from __future__ import annotations

from dataclasses import dataclass

from .models import PositionSnapshot, ThesisState


@dataclass(frozen=True)
class ProtectionPlan:
    """Desired broker protection expressed in R, never raw magic dollars."""

    floor_r: float | None
    giveback_allowance_r: float | None
    reason: str


class AdaptiveProtectionPlanner:
    """Protect gains without mechanically cutting the right tail.

    The floor is monotonic when applied by the execution adapter. Healthy,
    strengthening runners receive more breathing room; weakening runners give
    back less. No fixed +1R/+2R take-profit exists here.
    """

    def plan(self, snapshot: PositionSnapshot) -> ProtectionPlan:
        peak = max(0.0, snapshot.peak_r)
        if peak < 0.75:
            return ProtectionPlan(None, None, "PIS_NOT_YET_PROVEN")

        health = snapshot.health if snapshot.health is not None else 50.0
        if snapshot.thesis_state is ThesisState.STRENGTHENING and health >= 75.0:
            # Preserve convexity: exceptional winners need room to become 5R/8R/10R.
            giveback = max(0.80, min(2.50, peak * 0.42))
        elif snapshot.thesis_state is ThesisState.STABLE and health >= 60.0:
            giveback = max(0.65, min(1.75, peak * 0.34))
        elif snapshot.thesis_state is ThesisState.WEAKENING:
            giveback = max(0.35, min(0.90, peak * 0.22))
        else:
            giveback = max(0.25, min(0.65, peak * 0.18))

        floor_r = peak - giveback
        # Before a winner has enough excursion, avoid manufacturing a tight
        # breakeven stop that normal noise can hit. Once sufficiently proven,
        # never intentionally convert a large winner back into a full loser.
        if peak < 1.25:
            floor_r = min(floor_r, 0.0)
        elif peak >= 2.0:
            floor_r = max(0.25, floor_r)

        return ProtectionPlan(round(floor_r, 4), round(giveback, 4), "PIS_ADAPTIVE_CONVEX_PROTECTION")
