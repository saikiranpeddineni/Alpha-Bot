"""Position Intelligence System (PIS)

Single source of truth for dynamic position health tracking, adaptive protection,
runner intelligence, and discretionary exit management.

Broker-agnostic core. Deterministic, explainable, and thread-safe.
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
from app.pis.engine_adapter import PISEngineAdapter
from app.pis.state_machine import PositionLifecycleStateMachine, InvalidLifecycleTransitionError

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
