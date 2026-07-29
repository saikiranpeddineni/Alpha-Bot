from __future__ import annotations

from .models import ExitAction, LifecycleState, PISDecision, PositionSnapshot, ThesisState


class PISExitPlanner:
    """Single owner of discretionary exit policy under CR-290.

    This planner does not replace broker/risk safety invariants. Catastrophic
    protection, reconciliation, kill-switch liquidation and mandatory EOD
    flattening remain outside discretionary exit policy.
    """

    policy_version = "pis-v1"

    def decide(self, snapshot: PositionSnapshot) -> PISDecision:
        health = snapshot.health
        if health is None or snapshot.thesis_state is ThesisState.UNKNOWN:
            return PISDecision(
                ExitAction.INSUFFICIENT_EVIDENCE,
                0.0,
                ("PIS_EVIDENCE_INCOMPLETE",),
                snapshot,
                self.policy_version,
            )

        if snapshot.lifecycle is LifecycleState.EXIT_CANDIDATE or snapshot.thesis_state is ThesisState.BROKEN:
            confidence = min(100.0, max(70.0, 100.0 - health))
            return PISDecision(
                ExitAction.EXIT,
                confidence,
                ("PIS_THESIS_BROKEN", "PIS_HEALTH_CRITICAL"),
                snapshot,
                self.policy_version,
            )

        if snapshot.lifecycle is LifecycleState.WEAKENING:
            # Do not turn ordinary noise into a realized loss. PIS v1 exits a
            # weakening thesis only after material deterioration; otherwise it
            # requests tighter protection and waits for confirmation.
            if health < 42.0 and snapshot.thesis_drift is not None and snapshot.thesis_drift <= -20.0:
                return PISDecision(
                    ExitAction.EXIT,
                    min(95.0, 60.0 + abs(snapshot.thesis_drift)),
                    ("PIS_PERSISTENT_DETERIORATION",),
                    snapshot,
                    self.policy_version,
                )
            return PISDecision(
                ExitAction.TIGHTEN_PROTECTION,
                65.0,
                ("PIS_THESIS_WEAKENING",),
                snapshot,
                self.policy_version,
            )

        if snapshot.lifecycle is LifecycleState.RUNNER:
            # A fixed +1R/+2R target is intentionally not an exit condition.
            # Healthy runners stay alive and transition to trailing protection.
            return PISDecision(
                ExitAction.TRAIL,
                min(95.0, max(70.0, health)),
                ("PIS_RUNNER_HEALTHY", "PIS_LET_WINNER_RUN"),
                snapshot,
                self.policy_version,
            )

        if snapshot.thesis_state is ThesisState.STRENGTHENING:
            return PISDecision(
                ExitAction.HOLD,
                min(95.0, max(60.0, health)),
                ("PIS_THESIS_STRENGTHENING",),
                snapshot,
                self.policy_version,
            )

        return PISDecision(
            ExitAction.KEEP_PLAN,
            min(90.0, max(50.0, health)),
            ("PIS_THESIS_STABLE",),
            snapshot,
            self.policy_version,
        )
