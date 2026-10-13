"""PIS Core Data Models and Configuration.

Defines immutable observations, decision structures, lifecycle states,
reason codes, and strict configuration validation for PIS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


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


class MarketRegime(str, Enum):
    """Macro and microstructure market regime classification."""

    TREND_RUNNER = "TREND_RUNNER"
    RANGE_BOUND = "RANGE_BOUND"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    CHOPPY_DEFENSIVE = "CHOPPY_DEFENSIVE"


class ReasonCode(str, Enum):
    """Structured, explainable reason codes for PIS decisions."""

    INITIAL_ENTRY = "INITIAL_ENTRY"
    HEALTHY_TREND = "HEALTHY_TREND"
    RUNNER_QUALIFIED = "RUNNER_QUALIFIED"
    SCALE_OUT_TARGET = "SCALE_OUT_TARGET"
    PROFIT_GIVEBACK_GUARD = "PROFIT_GIVEBACK_GUARD"
    PROTECTION_RATCHET = "PROTECTION_RATCHET"
    THESIS_DEGRADED = "THESIS_DEGRADED"
    EARLY_THESIS_INVALIDATED = "EARLY_THESIS_INVALIDATED"
    HEALTH_EXHAUSTION = "HEALTH_EXHAUSTION"
    MAX_R_CEILING_REACHED = "MAX_R_CEILING_REACHED"
    DISTRIBUTION_DETECTED = "DISTRIBUTION_DETECTED"
    PROTECTION_BREACH = "PROTECTION_BREACH"
    FLOOR_TAG_RUNNER = "FLOOR_TAG_RUNNER"
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
    current_stop: float | None = None
    take_profit: float | None = None
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
    early_invalidation_detected: bool = False


@dataclass(frozen=True)
class EvidenceCoverage:
    """Coverage and confidence metrics for decision explainability."""

    bars_observed: int
    data_quality_score: float
    confidence: float
    evidence_factors: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PISDecision:
    """Immutable output decision produced by PIS evaluation."""

    timestamp: datetime
    symbol: str
    action: ExitAction
    target_stop_loss: float | None
    exit_ratio: float
    reason_codes: list[ReasonCode]
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
    market_regime: MarketRegime = MarketRegime.TREND_RUNNER
    brainstorm_narrative: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotHistory:
    """Immutable sequence snapshot for audit or replay."""

    snapshots: tuple[PositionObservation, ...]

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
    activation_mfe_r: float = 1.50
    runner_qualification_r: float = 3.0
    giveback_pct_runner: float = 0.35
    giveback_pct_hyper_runner_min: float = 0.10
    hyper_runner_decay_rate: float = 0.05
    giveback_pct_maturing: float = 0.25
    min_breathing_room_r: float = 0.35
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
    scale_out_target_r: float = 2.00
    # --- Progressive profit lock (sub-activation zone) ---
    # Live winners average ~0.44R to 1.5R peaks. Sub-activation arms once peak reaches
    # breakeven_trigger_r (0.40R) and 2.5x ATR confirmation, capped at progressive_lock_cap_trigger_r (0.85R)
    # so high ATR names are never permanently disarmed. Locks a fraction of peak profit toward activation_mfe_r.
    breakeven_trigger_r: float = 0.40
    breakeven_trigger_atr_mult: float = 2.50
    progressive_lock_cap_trigger_r: float = 0.85
    progressive_lock_start_frac: float = 0.20
    progressive_lock_end_frac: float = 0.35
    # The locked floor may never trail closer than this many ATRs behind the
    # peak -- banking profit is pointless if normal churn stops you out one
    # bar later.
    progressive_lock_trail_atr_mult: float = 1.00
    # --- Profit giveback guard ---
    # When a trade peaked above profit_guard_peak_r and then surrendered more
    # than profit_guard_max_giveback_pct of that peak while still green, bank a
    # partial immediately instead of waiting for the trailing floor.
    enable_profit_giveback_guard: bool = True
    profit_guard_peak_r: float = 0.60
    profit_guard_max_giveback_pct: float = 0.55
    # A giveback smaller than this many ATRs is noise, not reversal.
    profit_guard_min_giveback_atr_r: float = 0.75
    profit_guard_exit_ratio: float = 0.50
    # --- Trend-quality adaptive scale-out ---
    # Strong trends keep the full scale_out_target_r; weakening trends bank at
    # weak_trend_scale_out_r instead (1R) so soft runners don't round-trip.
    weak_trend_scale_out_r: float = 1.00
    weak_momentum_threshold: float = 0.45
    # --- Floor-tag runner (evidence-driven) ---
    # Measured on live history: winners average only ~0.44R peak, so the 2R
    # scale-out gate almost never arms and EVERY protection-floor tag became a
    # full exit -- while 38% of exits were followed by >= +1R continuation
    # (~696 R left behind across 29 sessions). When the floor is tagged but the
    # thesis is NOT invalidated, exit only this fraction and let the remainder
    # ride with the same floor as its stop. The second floor tag (or any thesis
    # degradation) still exits everything.
    enable_floor_tag_runner: bool = True
    floor_tag_exit_ratio: float = 0.50
    runner_wide_atr_mult: float = 2.00
    enable_dynamic_max_r: bool = True
    max_r_cap: float = 8.00
    enable_market_regime_awareness: bool = True
    enable_early_invalidation: bool = True
    early_invalidation_min_bars: int = 2
    early_invalidation_max_bars: int = 5
    # A discretionary thesis scratch requires more than one adverse print.
    # Broker protection remains the hard loss boundary; this confirmation
    # window prevents a single noisy candle from becoming a panic exit.
    early_invalidation_confirmation_bars: int = 2
    early_invalidation_r_threshold: float = -0.55
    enable_trade_reasoning_narrative: bool = True

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
        if not (0.0 < self.breakeven_trigger_r < self.activation_mfe_r):
            raise ValueError(
                f"breakeven_trigger_r must be between 0 and activation_mfe_r ({self.activation_mfe_r}), got {self.breakeven_trigger_r}"
            )
        if self.breakeven_trigger_atr_mult < 0.0:
            raise ValueError(f"breakeven_trigger_atr_mult must be >= 0, got {self.breakeven_trigger_atr_mult}")
        if self.progressive_lock_trail_atr_mult <= 0.0:
            raise ValueError(f"progressive_lock_trail_atr_mult must be > 0, got {self.progressive_lock_trail_atr_mult}")
        if self.profit_guard_min_giveback_atr_r < 0.0:
            raise ValueError(f"profit_guard_min_giveback_atr_r must be >= 0, got {self.profit_guard_min_giveback_atr_r}")
        if not (0.0 < self.progressive_lock_start_frac <= self.progressive_lock_end_frac < 1.0):
            raise ValueError("progressive lock fractions must satisfy 0 < start <= end < 1")
        if not (0.0 < self.profit_guard_max_giveback_pct < 1.0):
            raise ValueError(f"profit_guard_max_giveback_pct must be between 0 and 1, got {self.profit_guard_max_giveback_pct}")
        if self.weak_trend_scale_out_r <= 0.0 or self.weak_trend_scale_out_r > self.scale_out_target_r:
            raise ValueError(f"weak_trend_scale_out_r must be in (0, scale_out_target_r], got {self.weak_trend_scale_out_r}")
        if not (0.0 < self.floor_tag_exit_ratio < 1.0):
            raise ValueError(f"floor_tag_exit_ratio must be between 0 and 1, got {self.floor_tag_exit_ratio}")
        if self.runner_wide_atr_mult <= 0.0:
            raise ValueError(f"runner_wide_atr_mult must be > 0, got {self.runner_wide_atr_mult}")
        if self.early_invalidation_confirmation_bars < 1:
            raise ValueError(
                "early_invalidation_confirmation_bars must be >= 1, "
                f"got {self.early_invalidation_confirmation_bars}"
            )

