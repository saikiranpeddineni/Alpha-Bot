"""CR-290 Position Intelligence System.

PIS is the single policy owner for discretionary position exits. Broker-level
catastrophic protection, reconciliation, kill switches, and mandatory end-of-day
liquidation remain safety mechanisms rather than competing exit strategies.
"""

from .authority import ExitAuthority, ExitAuthorityViolation, assert_close_authorized
from .engine_adapter import PISEngineAdapter, PISEvaluation
from .evidence import observation_from_market_data
from .execution import ExecutionIntent, PISExecutionAuthority
from .models import (
    ExitAction,
    LifecycleState,
    PositionObservation,
    PositionSnapshot,
    PISDecision,
    ThesisState,
)
from .position_brain import PositionBrain
from .protection import AdaptiveProtectionPlanner, ProtectionPlan
from .service import PositionIntelligenceSystem

__all__ = [
    "AdaptiveProtectionPlanner",
    "ExecutionIntent",
    "ExitAction",
    "ExitAuthority",
    "ExitAuthorityViolation",
    "LifecycleState",
    "PISDecision",
    "PISEngineAdapter",
    "PISEvaluation",
    "PISExecutionAuthority",
    "PositionBrain",
    "PositionIntelligenceSystem",
    "PositionObservation",
    "PositionSnapshot",
    "ProtectionPlan",
    "ThesisState",
    "assert_close_authorized",
    "observation_from_market_data",
]
