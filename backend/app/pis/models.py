from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping


class ThesisState(str, Enum):
    STRENGTHENING = "STRENGTHENING"
    STABLE = "STABLE"
    WEAKENING = "WEAKENING"
    BROKEN = "BROKEN"
    UNKNOWN = "UNKNOWN"


class LifecycleState(str, Enum):
    NEW = "NEW"
    DISCOVERY = "DISCOVERY"
    VALIDATED = "VALIDATED"
    ACCELERATING = "ACCELERATING"
    RUNNER = "RUNNER"
    MATURING = "MATURING"
    WEAKENING = "WEAKENING"
    DISTRIBUTION = "DISTRIBUTION"
    EXIT_CANDIDATE = "EXIT_CANDIDATE"
    CLOSED = "CLOSED"


class ExitAction(str, Enum):
    HOLD = "HOLD"
    KEEP_PLAN = "KEEP_PLAN"
    TIGHTEN_PROTECTION = "TIGHTEN_PROTECTION"
    TRAIL = "TRAIL"
    HARVEST_PARTIAL = "HARVEST_PARTIAL"
    EXIT = "EXIT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class PositionObservation:
    """Point-in-time evidence supplied to PIS. Values are normalized to 0..100."""

    timestamp: datetime
    price: float
    trend: float | None = None
    momentum: float | None = None
    volume: float | None = None
    relative_strength: float | None = None
    market_context: float | None = None
    atr: float | None = None
    vwap_distance_atr: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionSnapshot:
    timestamp: datetime
    symbol: str
    side: str
    lifecycle: LifecycleState
    current_r: float
    peak_r: float
    trough_r: float
    giveback_r: float
    health: float | None
    entry_health: float | None
    thesis_drift: float | None
    thesis_state: ThesisState
    components: Mapping[str, float]


@dataclass(frozen=True)
class PISDecision:
    action: ExitAction
    confidence: float
    reason_codes: tuple[str, ...]
    snapshot: PositionSnapshot
    policy_version: str = "pis-v1"

    @property
    def requests_exit(self) -> bool:
        return self.action is ExitAction.EXIT
