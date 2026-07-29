"""CR-290 Position Intelligence System.

PIS is the single policy owner for discretionary position exits. Broker-level
catastrophic protection, reconciliation, kill switches, and mandatory end-of-day
liquidation remain safety mechanisms rather than competing exit strategies.
"""

from .evidence import observation_from_market_data
from .models import (
    ExitAction,
    LifecycleState,
    PositionObservation,
    PositionSnapshot,
    PISDecision,
    ThesisState,
)
from .position_brain import PositionBrain
from .service import PositionIntelligenceSystem

__all__ = [
    "ExitAction",
    "LifecycleState",
    "PositionObservation",
    "PositionSnapshot",
    "PISDecision",
    "PositionBrain",
    "PositionIntelligenceSystem",
    "ThesisState",
    "observation_from_market_data",
]
