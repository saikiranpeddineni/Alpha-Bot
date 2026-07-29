from __future__ import annotations

from dataclasses import dataclass

from .models import ExitAction, PISDecision
from .protection import AdaptiveProtectionPlanner, ProtectionPlan


@dataclass(frozen=True)
class ExecutionIntent:
    close_position: bool
    protection: ProtectionPlan | None
    suppress_legacy_exit: bool
    reason: str


class PISExecutionAuthority:
    """The only translation layer from PIS policy to discretionary execution.

    Legacy strategy EXIT, fixed take-profit, ATR trailing and old harvesting
    signals are advisory inputs at most. They are never direct close authority.
    Hard safety liquidation is intentionally outside this class and cannot be
    vetoed by PIS.
    """

    def __init__(self) -> None:
        self.protection_planner = AdaptiveProtectionPlanner()

    def translate(self, decision: PISDecision) -> ExecutionIntent:
        if decision.action is ExitAction.EXIT:
            return ExecutionIntent(True, None, True, "PIS_CONFIRMED_EXIT")

        protection = None
        if decision.action in {ExitAction.TRAIL, ExitAction.TIGHTEN_PROTECTION}:
            protection = self.protection_planner.plan(decision.snapshot)

        return ExecutionIntent(
            close_position=False,
            protection=protection,
            suppress_legacy_exit=True,
            reason=f"PIS_{decision.action.value}",
        )

    @staticmethod
    def legacy_discretionary_exit_allowed() -> bool:
        """CR-290 invariant: no competing discretionary exit authority."""
        return False
