"""Position Intelligence System (PIS).

Single source of truth for dynamic position health tracking, adaptive protection,
runner intelligence, and discretionary exit management.
"""

from app.pis.models import (
    EvidenceCoverage,
    ExitAction,
    LifecycleState,
    PISConfig,
    PISDecision,
    PositionObservation,
    ReasonCode,
    SnapshotHistory,
    ThesisDrift,
    TradeHealth,
)
from app.pis.position_brain import PositionBrain
from app.pis.service import PISService
from app.pis.engine_adapter import PISEngineAdapter as _PISEngineAdapter
from app.pis.state_machine import PositionLifecycleStateMachine, InvalidLifecycleTransitionError


class PISEngineAdapter(_PISEngineAdapter):
    """Engine adapter with persisted, runtime-tunable PIS configuration.

    Existing callers can continue constructing the adapter with only ``mode``;
    when no explicit config is supplied, the adapter merges persisted ``pis_config``
    settings over the validated PIS defaults.
    """

    def __init__(self, service=None, config=None, mode: str = "live"):
        if config is None:
            overrides = {}
            try:
                from app.db.database import Database
                db = Database.active()
                stored = db.get_setting("pis_config") if db else None
                if isinstance(stored, dict):
                    overrides = stored
            except Exception:
                overrides = {}
            config = PISConfig(**overrides)
        super().__init__(service=service, config=config, mode=mode)


__all__ = [
    "EvidenceCoverage",
    "ExitAction",
    "LifecycleState",
    "PISConfig",
    "PISDecision",
    "PositionObservation",
    "ReasonCode",
    "SnapshotHistory",
    "ThesisDrift",
    "TradeHealth",
    "PositionBrain",
    "PISService",
    "PISEngineAdapter",
    "PositionLifecycleStateMachine",
    "InvalidLifecycleTransitionError",
]
