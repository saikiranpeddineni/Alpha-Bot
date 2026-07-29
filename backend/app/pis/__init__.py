"""CR-290 Position Intelligence System.

PIS is the single policy owner for discretionary position exits. Broker-level
catastrophic protection, reconciliation, kill switches, and mandatory end-of-day
liquidation remain safety mechanisms rather than competing exit strategies.
"""

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
    "LifecycleState",
    "PISDecision",
    "PISExecutionAuthority",
    "PositionBrain",
    "PositionIntelligenceSystem",
    "PositionObservation",
    "PositionSnapshot",
    "ProtectionPlan",
    "ThesisState",
    "observation_from_market_data",
]
