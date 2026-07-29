from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .authority import ExitAuthority, assert_close_authorized
from .engine_adapter import PISEngineAdapter, PISEvaluation


@dataclass(frozen=True)
class LegacyExitSignal:
    source: str
    reason: str


class PISLiveExitController:
    """CR-290 live cutover controller.

    Legacy strategy exits, fixed targets, ATR trailing exits and runner-manager
    exits are deliberately suppressed as execution authorities. They can be
    logged as counterfactual evidence while PIS owns the discretionary decision.
    """

    LEGACY_DISCRETIONARY_SOURCES = frozenset(
        {"strategy_exit", "fixed_take_profit", "atr_trailing", "live_runner", "adaptive_harvester"}
    )

    def __init__(self) -> None:
        self.adapter = PISEngineAdapter()

    def evaluate(self, **kwargs: Any) -> PISEvaluation:
        return self.adapter.evaluate(**kwargs)

    def authorize_pis_close(self, evaluation: PISEvaluation) -> None:
        assert_close_authorized(
            authority=ExitAuthority.PIS,
            pis_requests_exit=evaluation.decision.requests_exit,
        )

    def suppress_legacy(self, source: str, reason: str = "") -> LegacyExitSignal:
        if source not in self.LEGACY_DISCRETIONARY_SOURCES:
            raise ValueError(f"unknown legacy discretionary exit source: {source}")
        return LegacyExitSignal(source=source, reason=reason)

    def position_closed(self, symbol: str) -> None:
        self.adapter.position_closed(symbol)
