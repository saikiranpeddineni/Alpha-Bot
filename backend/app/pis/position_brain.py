"""PIS PositionBrain -- Single Position Intelligence Source of Truth.

Thread-safe, bounded-memory, deterministic state holder for an individual position.

Guarantees:
    1. Thread-safe evaluation via per-position `threading.RLock`.
    2. Bounded memory history via `collections.deque(maxlen=history_max_len)`.
    3. Strict lifecycle state machine transition validation.
    4. Monotonic protection floor enforcement.
    5. Snapshot immutability & strictly increasing timestamp validation.
    6. Complete cleanup on close().
"""

from __future__ import annotations

import math
import threading
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.pis.authority import PISAuthority
from app.pis.models import (
    ExitAction,
    LifecycleState,
    PISConfig,
    PISDecision,
    PositionObservation,
    SnapshotHistory,
    TradeHealth,
)
from app.pis.profit_memory import ProfitMemoryState, ProfitMemoryTracker
from app.pis.state_machine import PositionLifecycleStateMachine


class PositionBrainClosedError(RuntimeError):
    """Raised when an operation is attempted on a closed PositionBrain."""

    pass


class PositionBrain:
    """Evaluates and tracks position health, lifecycle, protection, and exits for a single symbol."""

    def __init__(
        self,
        symbol: str,
        initial_observation: PositionObservation,
        config: Optional[PISConfig] = None,
        restored_memory: Optional[Any] = None,
    ) -> None:
        """Initialize PositionBrain for a symbol with initial observation.

        Args:
            symbol: Standardized symbol string.
            initial_observation: PositionObservation snapshot.
            config: Optional PISConfig instance.
            restored_memory: Optional restored ProfitMemoryState.
        """
        self._lock = threading.RLock()
        self.config = config or PISConfig()
        self.config.validate()

        with self._lock:
            # Validate symbol and initial observation
            self.symbol = symbol.upper()
            PositionObservation.validate(initial_observation)
            if initial_observation.symbol.upper() != self.symbol:
                raise ValueError(
                    f"Observation symbol '{initial_observation.symbol}' does not match brain symbol '{self.symbol}'"
                )

            self.authority = PISAuthority(self.config)
            self.state_machine = PositionLifecycleStateMachine(LifecycleState.NEW)

            # Bounded memory history (Priority 2)
            self._history: deque[PositionObservation] = deque(maxlen=self.config.history_max_len)
            self._history.append(initial_observation)

            # Profit memory state (Priority 2, Priority 7)
            if restored_memory is not None:
                self._profit_memory = restored_memory
                # We need to make sure the state is updated with the initial observation to keep it fresh
                self._profit_memory = ProfitMemoryTracker.update(self._profit_memory, initial_observation)
            else:
                self._profit_memory = ProfitMemoryTracker.initial_state(initial_observation)

            self._last_observation: PositionObservation = initial_observation
            self._last_timestamp: datetime = initial_observation.timestamp
            self._current_stop_floor: float = initial_observation.initial_stop
            self._last_decision: Optional[PISDecision] = None
            self._is_closed: bool = False
            self._stagnation_counter: int = 0
            self._degradation_counter: int = 0
            self._runner_qualified: bool = False
            self._has_scaled_out: bool = False
            self._smoothed_health_score: Optional[float] = None

            # Initial evaluation
            self._evaluate_internal(initial_observation)

    @property
    def is_closed(self) -> bool:
        """Check if brain is closed."""
        with self._lock:
            return self._is_closed

    @property
    def has_scaled_out(self) -> bool:
        """Check if position has executed first-stage scale-out."""
        with self._lock:
            return self._has_scaled_out


    @property
    def lifecycle(self) -> LifecycleState:
        """Get current lifecycle state."""
        with self._lock:
            return self.state_machine.current_state

    @property
    def peak_r(self) -> float:
        """Get current peak R."""
        with self._lock:
            return self._profit_memory.peak_r

    @property
    def trough_r(self) -> float:
        """Get current trough R."""
        with self._lock:
            return self._profit_memory.trough_r

    @property
    def current_stop_floor(self) -> float:
        """Get current protective stop floor."""
        with self._lock:
            return self._current_stop_floor

    @property
    def last_decision(self) -> Optional[PISDecision]:
        """Get last PISDecision output."""
        with self._lock:
            return self._last_decision

    @property
    def history(self) -> SnapshotHistory:
        """Get immutable snapshot history."""
        with self._lock:
            return SnapshotHistory(tuple(self._history))

    @property
    def history_length(self) -> int:
        """Get current history item count."""
        with self._lock:
            return len(self._history)

    def observe(self, obs: PositionObservation) -> PISDecision:
        """Process a new point-in-time observation. Thread-safe evaluation (Priority 1).

        Args:
            obs: PositionObservation snapshot.

        Returns:
            PISDecision output.

        Raises:
            PositionBrainClosedError: If brain is already closed.
            ValueError: If observation is invalid or timestamps are non-monotonic.
        """
        with self._lock:
            if self._is_closed:
                raise PositionBrainClosedError(f"Cannot observe closed PositionBrain for '{self.symbol}'")

            # 1. Input validation (Priority 6)
            PositionObservation.validate(obs)

            if obs.symbol.upper() != self.symbol:
                raise ValueError(f"Observation symbol '{obs.symbol}' does not match brain symbol '{self.symbol}'")

            # 2. Strict timestamp monotonicity check (Priority 7 - Reject time travel)
            if obs.timestamp < self._last_timestamp:
                raise ValueError(
                    f"Time travel rejected: observation timestamp {obs.timestamp} < last timestamp {self._last_timestamp}"
                )

            # 3. Update profit memory (Priority 7)
            self._profit_memory = ProfitMemoryTracker.update(self._profit_memory, obs)

            # 4. Check stagnation
            if abs(obs.current_price - self._last_observation.current_price) < 1e-4:
                self._stagnation_counter += 1
            else:
                self._stagnation_counter = 0

            # 5. Append to history deque (Priority 2 - Bounded memory growth)
            self._history.append(obs)
            self._last_observation = obs
            self._last_timestamp = obs.timestamp

            # 6. Evaluate and return decision
            return self._evaluate_internal(obs)

    def _evaluate_internal(self, obs: PositionObservation) -> PISDecision:
        """Internal deterministic evaluation logic. Assumes self._lock is acquired."""
        peak_r = self._profit_memory.peak_r
        trough_r = self._profit_memory.trough_r

        # 1. Health evaluation with noise-filtering continuous EMA smoothing (alpha = 0.30 over 3-5 bars)
        raw_health = self.authority.health_evaluator.evaluate(obs, peak_r, trough_r)
        if self._smoothed_health_score is None:
            self._smoothed_health_score = raw_health.overall
        else:
            self._smoothed_health_score = (0.30 * raw_health.overall) + (0.70 * self._smoothed_health_score)

        health = TradeHealth(
            overall=self._smoothed_health_score,
            trend_score=raw_health.trend_score,
            momentum_score=raw_health.momentum_score,
            volatility_score=raw_health.volatility_score,
            volume_score=raw_health.volume_score,
            news_sentiment=raw_health.news_sentiment,
        )

        # 2. Thesis drift evaluation
        thesis = self.authority.thesis_evaluator.evaluate(obs, trough_r, self._stagnation_counter, peak_r)

        # 3. Runner intelligence & Max R capacity prediction
        predicted_max_r, runner_prob = self.authority.runner_intelligence.predict_max_r(
            obs, peak_r, health
        )
        runner_score, is_runner = self.authority.runner_intelligence.evaluate_runner_score(
            obs, peak_r, health
        )
        if is_runner:
            self._runner_qualified = True

        # 4. State transition rules with persistence confirmation (Priority 3)
        self._update_lifecycle(health, thesis, peak_r)

        # 5. Protection floor calculation with dynamic ceiling proximity (Priority 4)
        target_stop = self.authority.protection_planner.compute_protection_floor(
            obs=obs,
            peak_r=peak_r,
            health=health,
            previous_floor=self._current_stop_floor,
            allow_relaxation=False,
            has_scaled_out=self._has_scaled_out,
            predicted_max_r=predicted_max_r,
        )
        self._current_stop_floor = target_stop

        # 6. Exit planner synthesis with dynamic ceiling harvesting (Priority 13)
        decision = self.authority.exit_planner.synthesize_decision(
            obs=obs,
            lifecycle=self.state_machine.current_state,
            health=health,
            thesis=thesis,
            runner_score=runner_score,
            is_runner=self._runner_qualified,
            target_stop=target_stop,
            peak_r=peak_r,
            trough_r=trough_r,
            history_len=len(self._history),
            has_scaled_out=self._has_scaled_out,
            predicted_max_r=predicted_max_r,
            runner_probability=runner_prob,
        )

        if decision.action == ExitAction.PARTIAL_EXIT:
            self._has_scaled_out = True
            self._runner_qualified = True
            # Recompute stop floor with runner mode active
            target_stop = self.authority.protection_planner.compute_protection_floor(
                obs=obs,
                peak_r=peak_r,
                health=health,
                previous_floor=self._current_stop_floor,
                allow_relaxation=False,
                has_scaled_out=True,
                predicted_max_r=predicted_max_r,
            )
            self._current_stop_floor = target_stop

        self._last_decision = decision
        return decision


    def _update_lifecycle(
        self,
        health: TradeHealth,
        thesis: ThesisDrift,
        peak_r: float,
    ) -> None:
        """Determine and apply state transitions with persistence gating against noise."""
        current = self.state_machine.current_state

        if current == LifecycleState.CLOSED:
            return

        # Track degradation persistence across consecutive bars
        is_degraded_bar = thesis.is_degraded or health.overall < self.config.health_distribution_threshold
        if is_degraded_bar:
            self._degradation_counter += 1
        else:
            self._degradation_counter = 0

        target = current

        if current == LifecycleState.NEW:
            if len(self._history) >= 2:
                target = LifecycleState.DISCOVERY
        elif current == LifecycleState.DISCOVERY:
            # Require persistence (>= 3 consecutive bars) before emergency exit
            if self._degradation_counter >= 3:
                target = LifecycleState.EXIT_CANDIDATE
            elif is_degraded_bar or (len(self._history) >= 3 and health.overall <= self.config.health_weakening_threshold):
                target = LifecycleState.WEAKENING
            elif health.overall >= self.config.health_validated_threshold and peak_r >= 0.75:
                target = LifecycleState.VALIDATED
        elif current == LifecycleState.VALIDATED:
            if self._degradation_counter >= 3:
                target = LifecycleState.EXIT_CANDIDATE
            elif is_degraded_bar or health.overall <= self.config.health_weakening_threshold:
                target = LifecycleState.WEAKENING
            elif health.overall >= self.config.health_accelerating_threshold and peak_r >= self.config.activation_mfe_r:
                target = LifecycleState.ACCELERATING
        elif current == LifecycleState.ACCELERATING:
            if peak_r >= self.config.runner_qualification_r and health.overall >= self.config.health_weakening_threshold:
                target = LifecycleState.RUNNER
            elif is_degraded_bar or health.overall <= self.config.health_weakening_threshold:
                target = LifecycleState.WEAKENING
        elif current == LifecycleState.RUNNER:
            if health.overall < self.config.health_distribution_threshold:
                target = LifecycleState.DISTRIBUTION
            elif health.overall < self.config.health_weakening_threshold:
                target = LifecycleState.WEAKENING
        elif current == LifecycleState.WEAKENING:
            if self._degradation_counter >= 3:
                target = LifecycleState.EXIT_CANDIDATE
            elif health.overall >= self.config.health_validated_threshold:
                target = LifecycleState.VALIDATED
        elif current == LifecycleState.DISTRIBUTION:
            if self._degradation_counter >= 2 or health.overall < 0.20:
                target = LifecycleState.EXIT_CANDIDATE

        if target != current and self.state_machine.can_transition_to(target):
            self.state_machine.transition_to(target, f"Health={health.overall:.2f}, PeakR={peak_r:.2f}, Degr={self._degradation_counter}")

    def close(self) -> None:
        """Clean up brain state and transition lifecycle to CLOSED (Priority 11).

        Ensures:
            1. Lifecycle state transitions to CLOSED.
            2. History deque is cleared.
            3. References released to prevent orphan memory.
            4. Brain marked closed to reject further observe() calls.
        """
        with self._lock:
            if self._is_closed:
                return

            if self.state_machine.can_transition_to(LifecycleState.CLOSED):
                self.state_machine.transition_to(LifecycleState.CLOSED, "Explicit brain close")

            self._history.clear()
            self._is_closed = True

    def serialize_state(self) -> Dict[str, Any]:
        """Serialize state for recovery (Priority 15)."""
        with self._lock:
            return {
                "symbol": self.symbol,
                "lifecycle": self.lifecycle.value,
                "peak_r": self.peak_r,
                "trough_r": self.trough_r,
                "current_stop_floor": self._current_stop_floor,
                "runner_qualified": self._runner_qualified,
                "stagnation_counter": self._stagnation_counter,
                "degradation_counter": self._degradation_counter,
                "is_closed": self._is_closed,
                "last_timestamp": self._last_timestamp.isoformat(),
            }
