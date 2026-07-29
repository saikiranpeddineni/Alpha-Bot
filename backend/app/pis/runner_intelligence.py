from __future__ import annotations

from dataclasses import dataclass

from .models import PositionSnapshot, ThesisState
from .profit_memory import ProfitMemory


@dataclass(frozen=True)
class RunnerAssessment:
    continuation_score: float
    runner: bool
    exceptional_runner: bool
    reason_codes: tuple[str, ...]


class RunnerIntelligence:
    """Score whether right-tail continuation deserves more time."""

    def __init__(self) -> None:
        self.memory = ProfitMemory()

    def assess(self, snapshot: PositionSnapshot) -> RunnerAssessment:
        health = snapshot.health if snapshot.health is not None else 0.0
        pullback = self.memory.assess(snapshot)
        score = 0.55 * health
        score += min(25.0, max(0.0, snapshot.peak_r) * 6.0)
        if snapshot.thesis_state is ThesisState.STRENGTHENING:
            score += 15.0
        elif snapshot.thesis_state is ThesisState.WEAKENING:
            score -= 18.0
        elif snapshot.thesis_state is ThesisState.BROKEN:
            score -= 35.0
        if pullback.healthy_pullback:
            score += 8.0
        if pullback.distribution_risk:
            score -= 20.0
        score = max(0.0, min(100.0, score))
        runner = snapshot.peak_r >= 1.5 and score >= 68.0
        exceptional = snapshot.peak_r >= 3.0 and score >= 80.0
        reasons = ["PIS_RUNNER_SCORE"]
        if runner:
            reasons.append("PIS_CONTINUATION_FAVORED")
        if exceptional:
            reasons.append("PIS_EXCEPTIONAL_RUNNER")
        reasons.extend(pullback.reason_codes)
        return RunnerAssessment(round(score, 4), runner, exceptional, tuple(reasons))
