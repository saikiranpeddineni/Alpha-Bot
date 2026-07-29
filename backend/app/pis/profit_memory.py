from __future__ import annotations

from dataclasses import dataclass

from .models import PositionSnapshot, ThesisState


@dataclass(frozen=True)
class PullbackAssessment:
    healthy_pullback: bool
    distribution_risk: bool
    giveback_fraction: float
    reason_codes: tuple[str, ...]


class ProfitMemory:
    """Interpret giveback in context instead of treating every retracement as failure."""

    def assess(self, snapshot: PositionSnapshot) -> PullbackAssessment:
        peak = max(0.0, snapshot.peak_r)
        giveback = max(0.0, snapshot.giveback_r)
        frac = giveback / peak if peak > 1e-9 else 0.0
        health = snapshot.health if snapshot.health is not None else 0.0

        healthy = (
            peak >= 1.0
            and frac <= 0.38
            and health >= 62.0
            and snapshot.thesis_state in {ThesisState.STABLE, ThesisState.STRENGTHENING}
        )
        distribution = (
            peak >= 1.25
            and frac >= 0.45
            and health < 55.0
            and snapshot.thesis_state in {ThesisState.WEAKENING, ThesisState.BROKEN}
        )

        reasons: list[str] = []
        if healthy:
            reasons.append("PIS_HEALTHY_PULLBACK")
        if distribution:
            reasons.append("PIS_DISTRIBUTION_RISK")
        if not reasons:
            reasons.append("PIS_PULLBACK_AMBIGUOUS")
        return PullbackAssessment(healthy, distribution, round(frac, 4), tuple(reasons))
