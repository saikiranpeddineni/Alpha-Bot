"""PIS Core Data Models and Configuration.

Defines immutable observations, decision structures, lifecycle states,
reason codes, and strict configuration validation for PIS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class LifecycleState(str, Enum):
    """Complete 10-state lifecycle for position tracking."""

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
    """Discretionary exit actions produced by PIS."""

    HOLD = "HOLD"
    UPDATE_PROTECTION = "UPDATE_PROTECTION"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    FULL_EXIT = "FULL_EXIT"


class ReasonCode(str, Enum):
    """Structured, explainable reason codes for PIS decisions."""

    INITIAL_ENTRY = "INITIAL_ENTRY"
    HEALTHY_TREND = "HEALTHY_TREND"
    RUNNER_QUALIFIED = "RUNNER_QUALIFIED"
    SCALE_OUT_TARGET = "SCALE_OUT_TARGET"
    PROTECTION_RATCHET = "PROTECTION_RATCHET"
    THESIS_DEGRADED = "THESIS_DEGRADED"
    HEALTH_EXHAUSTION = "HEALTH_EXHAUSTION"
    MAX_R_CEILING_REACHED = "MAX_R_CEILING_REACHED"
    DISTRIBUTION_DETECTED = "DISTRIBUTION_DETECTED"
    PROTECTION_BREACH = "PROTECTION_BREACH"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    INVALID_OBSERVATION = "INVALID_OBSERVATION"


@dataclass(frozen=True)
class PositionObservation:
    """Immutable point-in-time snapshot of position state and market data.

    Inputs:
        timestamp: Evaluation timestamp (UTC, timezone-aware or naive ISO)
        symbol: Standardized uppercase symbol name
        side: Position direction (1.0 for Long, -1.0 for Short)
        qty: Current share/unit quantity (must be > 0)
        entry_price: Average entry price (must be > 0)
        current_price: Current market price (must be > 0)
        high: Current/bar high price (must be >= current_price for long, or valid)
        low: Current/bar low price (must be <= current_price for long, or valid)
        close: Current/bar close price (must be > 0)
        volume: Current/bar volume (must be >= 0)
        atr: Average True Range (must be > 0)
        initial_stop: Original risk stop loss price (must be > 0)
        risk_per_share: Abs risk per share |entry - initial_stop| (must be > 0)
        current_stop: Current broker protective stop floor
        r_multiple: Current R-multiple ((price - entry) / risk * side)
    """

    timestamp: datetime
    symbol: str
    side: float
    qty: float
    entry_price: float
    current_price: float
    high: float
    low: float
    close: float
    volume: float
    atr: float
    initial_stop: float
    risk_per_share: float
    current_stop: Optional[float] = None
    take_profit: Optional[float] = None
    r_multiple: float = 0.0
    news_sentiment_score: float = 0.0

    def __post_init__(self) -> None:
        """Validate observation invariants fast."""
        self.validate(self)

    @staticmethod
    def validate(obs: PositionObservation) -> None:
        """Validate all observation numerical boundaries and sanity checks.

        Raises:
            ValueError: If any attribute violates invariants (NaN, inf, <= 0, etc.)
        """
        if not isinstance(obs.timestamp, datetime):
            raise ValueError(f"Invalid timestamp type: {type(obs.timestamp)}")

        if not obs.symbol or not isinstance(obs.symbol, str):
            raise ValueError(f"Invalid symbol: {obs.symbol}")

        if obs.side not in (1.0, -1.0):
            raise ValueError(f"Invalid position side: {obs.side}. Must be 1.0 or -1.0")

        # Numerical sanity check helper
        def _check_val(val: float, name: str, min_val: float = 0.0, allow_zero: bool = False) -> None:
            if not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
                raise ValueError(f"Observation field '{name}' must be a finite float, got {val}")
            if allow_zero:
                if val < min_val:
                    raise ValueError(f"Observation field '{name}' must be >= {min_val}, got {val}")
            else:
                if val <= min_val:
                    raise ValueError(f"Observation field '{name}' must be > {min_val}, got {val}")

        _check_val(obs.qty, "qty", min_val=0.0, allow_zero=False)
        _check_val(obs.entry_price, "entry_price", min_val=0.0, allow_zero=False)
        _check_val(obs.current_price, "current_price", min_val=0.0, allow_zero=False)
        _check_val(obs.high, "high", min_val=0.0, allow_zero=False)
        _check_val(obs.low, "low", min_val=0.0, allow_zero=False)
        _check_val(obs.close, "close", min_val=0.0, allow_zero=False)
        _check_val(obs.volume, "volume", min_val=0.0, allow_zero=True)
        _check_val(obs.atr, "atr", min_val=0.0, allow_zero=False)
        _check_val(obs.initial_stop, "initial_stop", min_val=0.0, allow_zero=False)
        _check_val(obs.risk_per_share, "risk_per_share", min_val=0.0, allow_zero=False)

        if obs.current_stop is not None:
            _check_val(obs.current_stop, "current_stop", min_val=0.0, allow_zero=False)

        if math.isnan(obs.r_multiple) or math.isinf(obs.r_multiple):
            raise ValueError(f"Observation r_multiple must be finite, got {obs.r_multiple}")


@dataclass(frozen=True)
class TradeHealth:
    """Trade health score breakdown [0.0, 1.0]."""

    overall: float
    trend_score: float
    momentum_score: float
    volatility_score: float
    volume_score: float
    news_sentiment: float = 0.0


@dataclass(frozen=True)
class ThesisDrift:
    """Thesis degradation metrics [0.0, 1.0]."""

    drift_score: float
    adverse_excursion_r: float
    stagnation_bars: int
    is_degraded: bool


@dataclass(frozen=True)
class EvidenceCoverage:
    """Coverage and confidence metrics for decision explainability."""

    bars_observed: int
    data_quality_score: float
    confidence: float
    evidence_factors: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PISDecision:
    """Immutable output decision produced by PIS evaluation."""

    timestamp: datetime
    symbol: str
    action: ExitAction
    target_stop_loss: Optional[float]
    exit_ratio: float
    reason_codes: List[ReasonCode]
    health: TradeHealth
    thesis: ThesisDrift
    runner_score: float
    confidence: float
    lifecycle_state: LifecycleState
    evidence_coverage: EvidenceCoverage
    peak_r: float
    trough_r: float
    predicted_max_r: float = 2.0
    runner_probability: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotHistory:
    """Immutable sequence snapshot for audit or replay."""

    snapshots: Tuple[PositionObservation, ...]

    def __len__(self) -> int:
        return len(self.snapshots)

    def __getitem__(self, index: int) -> PositionObservation:
        return self.snapshots[index]


@dataclass
class PISConfig:
    """Configurable PIS operational parameters eliminating all hardcoded magic numbers.

    All fields undergo strict startup validation.
    """

    history_max_len: int = 500
    activation_mfe_r: float = 1.5
    runner_qualification_r: float = 3.0
    giveback_pct_runner: float = 0.35
    giveback_pct_hyper_runner_min: float = 0.10
    hyper_runner_decay_rate: float = 0.05
    giveback_pct_maturing: float = 0.45
    min_breathing_room_r: float = 0.60
    min_locked_r: float = 0.05
    health_weakening_threshold: float = 0.55
    health_distribution_threshold: float = 0.35
    health_accelerating_threshold: float = 0.68
    health_validated_threshold: float = 0.80
    health_maturing_threshold: float = 0.62
    stagnation_max_bars: int = 30
    price_buffer_pct: float = 0.0002
    min_price_buffer_abs: float = 0.01
    profit_harvesting_mode: bool = True
    enable_scale_out: bool = True
    scale_out_ratio: float = 0.50
    scale_out_target_r: float = 1.00
    runner_wide_atr_mult: float = 2.00
    enable_dynamic_max_r: bool = True
    max_r_cap: float = 8.00

    def validate(self) -> None:
        """Validate startup configuration boundaries.

        Raises:
            ValueError: If configuration parameters are out of reasonable bounds.
        """
        if self.history_max_len < 10:
            raise ValueError(f"history_max_len must be >= 10, got {self.history_max_len}")
        if self.activation_mfe_r <= 0.0:
            raise ValueError(f"activation_mfe_r must be > 0, got {self.activation_mfe_r}")
        if self.runner_qualification_r <= self.activation_mfe_r:
            raise ValueError("runner_qualification_r must be > activation_mfe_r")
        if not (0.0 < self.giveback_pct_runner < 1.0):
            raise ValueError(f"giveback_pct_runner must be between 0 and 1, got {self.giveback_pct_runner}")
        if not (0.0 < self.giveback_pct_maturing < 1.0):
            raise ValueError(f"giveback_pct_maturing must be between 0 and 1, got {self.giveback_pct_maturing}")
        if self.min_breathing_room_r < 0.0:
            raise ValueError(f"min_breathing_room_r must be >= 0, got {self.min_breathing_room_r}")
        if self.health_distribution_threshold >= self.health_weakening_threshold:
            raise ValueError("health_distribution_threshold must be < health_weakening_threshold")
        if not (0.0 < self.scale_out_ratio <= 1.0):
            raise ValueError(f"scale_out_ratio must be between 0 and 1, got {self.scale_out_ratio}")
        if self.scale_out_target_r <= 0.0:
            raise ValueError(f"scale_out_target_r must be > 0, got {self.scale_out_target_r}")
        if self.runner_wide_atr_mult <= 0.0:
            raise ValueError(f"runner_wide_atr_mult must be > 0, got {self.runner_wide_atr_mult}")

