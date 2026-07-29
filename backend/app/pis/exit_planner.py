from __future__ import annotations

from .models import ExitAction, LifecycleState, PISDecision, PositionSnapshot, ThesisState
from .profit_memory import ProfitMemory
from .runner_intelligence import RunnerIntelligence


class PISExitPlanner:
    """Single owner of discretionary exit policy under CR-290."""

    policy_version = "pis-v3-runner-intelligence"

    def __init__(self, *, exit_confirmation_bars: int = 2) -> None:
        self.exit_confirmation_bars = max(1, int(exit_confirmation_bars))
        self._critical_streak = 0
        self._weakening_streak = 0
        self.profit_memory = ProfitMemory()
        self.runner_intelligence = RunnerIntelligence()

    def decide(self, snapshot: PositionSnapshot) -> PISDecision:
        health = snapshot.health
        if health is None or snapshot.thesis_state is ThesisState.UNKNOWN:
            self._critical_streak = 0
            self._weakening_streak = 0
            return PISDecision(ExitAction.INSUFFICIENT_EVIDENCE, 0.0, ("PIS_EVIDENCE_INCOMPLETE",), snapshot, self.policy_version)

        pullback = self.profit_memory.assess(snapshot)
        runner = self.runner_intelligence.assess(snapshot)

        # Critical deterioration is still confirmation-gated. A previously
        # exceptional winner is not granted immunity if the thesis truly breaks.
        critical = snapshot.lifecycle is LifecycleState.EXIT_CANDIDATE or snapshot.thesis_state is ThesisState.BROKEN
        weakening = snapshot.lifecycle is LifecycleState.WEAKENING or snapshot.thesis_state is ThesisState.WEAKENING
        self._critical_streak = self._critical_streak + 1 if critical else 0
        self._weakening_streak = self._weakening_streak + 1 if weakening else 0

        if critical:
            if self._critical_streak < self.exit_confirmation_bars:
                return PISDecision(ExitAction.TIGHTEN_PROTECTION, 68.0, ("PIS_CRITICAL_AWAITING_CONFIRMATION",), snapshot, self.policy_version)
            return PISDecision(
                ExitAction.EXIT,
                min(100.0, max(75.0, 100.0 - health)),
                ("PIS_THESIS_BROKEN_CONFIRMED", "PIS_HEALTH_CRITICAL_CONFIRMED"),
                snapshot,
                self.policy_version,
            )

        # This is the anti-premature-exit rule: retracement from MFE is not a
        # sell signal when trend/thesis health says it is ordinary digestion.
        if pullback.healthy_pullback and runner.runner:
            return PISDecision(
                ExitAction.HOLD,
                min(98.0, max(72.0, runner.continuation_score)),
                ("PIS_HEALTHY_PULLBACK", "PIS_RUNNER_CONTINUATION", "PIS_DO_NOT_CHASE_MFE"),
                snapshot,
                self.policy_version,
            )

        if weakening:
            severe = health < 42.0 and snapshot.thesis_drift is not None and snapshot.thesis_drift <= -20.0
            if severe and self._weakening_streak >= self.exit_confirmation_bars + 1:
                return PISDecision(
                    ExitAction.EXIT,
                    min(95.0, 60.0 + abs(snapshot.thesis_drift)),
                    ("PIS_PERSISTENT_DETERIORATION_CONFIRMED",),
                    snapshot,
                    self.policy_version,
                )
            reasons = ["PIS_THESIS_WEAKENING", "PIS_NO_PREMATURE_EXIT"]
            if pullback.distribution_risk:
                reasons.append("PIS_DISTRIBUTION_RISK")
            return PISDecision(ExitAction.TIGHTEN_PROTECTION, 65.0, tuple(reasons), snapshot, self.policy_version)

        if runner.exceptional_runner:
            return PISDecision(
                ExitAction.TRAIL,
                min(98.0, max(80.0, runner.continuation_score)),
                ("PIS_EXCEPTIONAL_RUNNER", "PIS_PRESERVE_RIGHT_TAIL", "PIS_NO_FIXED_PROFIT_CAP"),
                snapshot,
                self.policy_version,
            )

        if snapshot.lifecycle is LifecycleState.RUNNER or runner.runner:
            return PISDecision(
                ExitAction.TRAIL,
                min(95.0, max(70.0, runner.continuation_score)),
                ("PIS_RUNNER_HEALTHY", "PIS_LET_WINNER_RUN"),
                snapshot,
                self.policy_version,
            )

        if snapshot.thesis_state is ThesisState.STRENGTHENING:
            return PISDecision(ExitAction.HOLD, min(95.0, max(60.0, health)), ("PIS_THESIS_STRENGTHENING",), snapshot, self.policy_version)

        return PISDecision(ExitAction.KEEP_PLAN, min(90.0, max(50.0, health)), ("PIS_THESIS_STABLE",), snapshot, self.policy_version)
