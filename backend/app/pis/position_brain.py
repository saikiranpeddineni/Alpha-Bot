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
from collections.abc import Mapping
from datetime import datetime
from typing import Any

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
        config: PISConfig | None = None,
        restored_memory: Any | None = None,
    ) -> None:
        """Initialize PositionBrain for a symbol with initial observation.

        Args:
            symbol: Standardized symbol string.
            initial_observation: PositionObservation snapshot.
            config: Optional PISConfig instance.
            restored_memory: Optional ProfitMemoryState or serialized PIS state mapping.
        """
        self._lock = threading.RLock()
        self.config = config or PISConfig()
        self.config.validate()

        with self._lock:
            restored_state = self._restored_state_mapping(restored_memory)

            # Validate symbol and initial observation
            self.symbol = symbol.upper()
            PositionObservation.validate(initial_observation)
            if initial_observation.symbol.upper() != self.symbol:
                raise ValueError(
                    f"Observation symbol '{initial_observation.symbol}' does not match brain symbol '{self.symbol}'"
                )

            self.authority = PISAuthority(self.config)
            self.state_machine = PositionLifecycleStateMachine(
                self._restored_lifecycle(restored_state)
            )

            # Bounded memory history (Priority 2)
            self._history: deque[PositionObservation] = deque(maxlen=self.config.history_max_len)
            self._history.append(initial_observation)

            # Profit memory state (Priority 2, Priority 7)
            restored_profit_memory = self._restored_profit_memory(restored_memory, restored_state, initial_observation)
            if restored_profit_memory is not None:
                self._profit_memory = restored_profit_memory
                # Update restored memory with the current completed observation so
                # recovery never loses a favorable/adverse print on the restart bar.
                self._profit_memory = ProfitMemoryTracker.update(self._profit_memory, initial_observation)
            else:
                self._profit_memory = ProfitMemoryTracker.initial_state(initial_observation)

            self._last_observation: PositionObservation = initial_observation
            self._last_timestamp: datetime = initial_observation.timestamp
            self._current_stop_floor: float = self._restored_stop_floor(
                restored_state, initial_observation
            )
            self._last_decision: PISDecision | None = None
            self._is_closed: bool = False
            self._stagnation_counter: int = self._restored_counter(restored_state, "stagnation_counter")
            self._degradation_counter: int = self._restored_counter(restored_state, "degradation_counter")
            self._has_scaled_out: bool = self._restored_bool(restored_state, "has_scaled_out")
            # A completed scale-out is itself durable evidence that the runner
            # was qualified, even for a legacy payload that omitted the second
            # flag.  Explicit runner qualification remains independently restorable.
            self._runner_qualified: bool = (
                self._restored_bool(restored_state, "runner_qualified") or self._has_scaled_out
            )
            self._smoothed_health_score: float | None = self._restored_health_score(restored_state)

            # Initial evaluation
            self._evaluate_internal(initial_observation)

    @staticmethod
    def _restored_state_mapping(restored_memory: Any | None) -> Mapping[str, Any]:
        """Return a safe mapping view for legacy or full persisted state.

        Older adapter payloads contain only profit-memory keys.  Newer payloads
        reuse that same JSON object and add the rest of the brain state, so both
        forms intentionally enter the same restore path.
        """
        if isinstance(restored_memory, Mapping):
            nested_state = restored_memory.get("pis_state")
            if isinstance(nested_state, Mapping):
                return nested_state
            return restored_memory
        return {}

    @staticmethod
    def _finite_float(value: Any, default: float | None = None) -> float | None:
        """Coerce a persisted numeric value without accepting NaN or infinity."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    @classmethod
    def _restored_profit_memory(
        cls,
        restored_memory: Any | None,
        state: Mapping[str, Any],
        obs: PositionObservation,
    ) -> ProfitMemoryState | None:
        """Build a validated ProfitMemoryState from either supported payload form."""
        if isinstance(restored_memory, ProfitMemoryState):
            peak_r = cls._finite_float(restored_memory.peak_r)
            trough_r = cls._finite_float(restored_memory.trough_r)
            mfe_price = cls._finite_float(restored_memory.mfe_price)
            mae_price = cls._finite_float(restored_memory.mae_price)
        else:
            memory_payload = state.get("profit_memory", state)
            if isinstance(memory_payload, ProfitMemoryState):
                peak_r = cls._finite_float(memory_payload.peak_r)
                trough_r = cls._finite_float(memory_payload.trough_r)
                mfe_price = cls._finite_float(memory_payload.mfe_price)
                mae_price = cls._finite_float(memory_payload.mae_price)
            elif isinstance(memory_payload, Mapping):
                peak_r = cls._finite_float(memory_payload.get("peak_r"))
                trough_r = cls._finite_float(memory_payload.get("trough_r"))
                mfe_price = cls._finite_float(memory_payload.get("mfe_price"))
                mae_price = cls._finite_float(memory_payload.get("mae_price"))
            else:
                return None

        if peak_r is None or trough_r is None:
            return None
        if mfe_price is None or mfe_price <= 0.0:
            mfe_price = obs.current_price
        if mae_price is None or mae_price <= 0.0:
            mae_price = obs.current_price
        if trough_r > peak_r:
            return None

        return ProfitMemoryState(
            peak_r=peak_r,
            trough_r=trough_r,
            mfe_price=mfe_price,
            mae_price=mae_price,
        )

    @classmethod
    def _restored_lifecycle(cls, state: Mapping[str, Any]) -> LifecycleState:
        """Restore an active lifecycle state, failing closed for invalid values."""
        raw_state = state.get("lifecycle")
        try:
            lifecycle = raw_state if isinstance(raw_state, LifecycleState) else LifecycleState(raw_state)
        except (TypeError, ValueError):
            return LifecycleState.NEW
        # A closed brain must never be reanimated from an active-position payload.
        return LifecycleState.NEW if lifecycle == LifecycleState.CLOSED else lifecycle

    @classmethod
    def _restored_stop_floor(
        cls,
        state: Mapping[str, Any],
        obs: PositionObservation,
    ) -> float:
        """Restore a directional, finite floor without weakening the initial stop."""
        baseline = obs.initial_stop
        broker_floor = cls._finite_float(obs.current_stop)
        if broker_floor is not None and broker_floor > 0.0:
            if obs.side > 0:
                baseline = max(baseline, broker_floor)
            else:
                baseline = min(baseline, broker_floor)

        floor = cls._finite_float(state.get("current_stop_floor"))
        if floor is None or floor <= 0.0:
            return baseline
        if obs.side > 0 and floor < baseline:
            return baseline
        if obs.side < 0 and floor > baseline:
            return baseline
        return floor

    @staticmethod
    def _restored_bool(state: Mapping[str, Any], key: str) -> bool:
        """Restore JSON booleans without treating arbitrary strings as truthy."""
        value = state.get(key)
        return value if isinstance(value, bool) else False

    @classmethod
    def _restored_counter(cls, state: Mapping[str, Any], key: str) -> int:
        """Restore a non-negative bounded counter from JSON state."""
        value = cls._finite_float(state.get(key))
        if value is None or value < 0.0:
            return 0
        return int(value)

    @classmethod
    def _restored_health_score(cls, state: Mapping[str, Any]) -> float | None:
        """Restore the health EMA only when it is a valid probability."""
        value = cls._finite_float(state.get("smoothed_health_score"))
        if value is None or not 0.0 <= value <= 1.0:
            return None
        return value

    @staticmethod
    def _completed_close_r(obs: PositionObservation) -> float:
        """Return R based on the completed bar close, never the live quote."""
        risk = obs.risk_per_share
        if risk <= 1e-8 or not math.isfinite(risk):
            return 0.0
        value = (obs.close - obs.entry_price) / risk * obs.side
        return value if math.isfinite(value) else 0.0

    def _causal_peak_capture_signal(
        self,
        obs: PositionObservation,
        peak_r: float,
    ) -> tuple[bool, float]:
        """Detect a confirmed high-water reversal from observed bars only.

        The latest completed bars must close monotonically against the position
        direction, and the latest completed close must have surrendered both the
        configured minimum R and percentage of the remembered high-water peak.
        No future bar, session summary, or end-of-day value participates.
        """
        if not getattr(self.config, "enable_causal_peak_capture", True):
            return False, 0.0

        confirmation_bars = max(
            1,
            int(getattr(self.config, "causal_peak_capture_confirmation_bars", 2)),
        )
        if len(self._history) < confirmation_bars + 1:
            return False, 0.0

        min_peak_r = getattr(self.config, "causal_peak_capture_min_peak_r", 1.00)
        if not math.isfinite(peak_r) or peak_r < min_peak_r:
            return False, 0.0

        history = tuple(self._history)
        recent = history[-confirmation_bars:]
        previous_close = history[-confirmation_bars - 1].close
        for bar in recent:
            if bar.side != obs.side:
                return False, 0.0
            # Strict adverse close-to-close confirmation prevents a single wick
            # or unchanged print from becoming a reversal signal.
            if obs.side > 0:
                if bar.close >= previous_close:
                    return False, 0.0
            elif bar.close <= previous_close:
                return False, 0.0
            previous_close = bar.close

        latest_close_r = self._completed_close_r(obs)
        giveback_r = max(0.0, peak_r - latest_close_r)
        giveback_threshold = max(
            getattr(self.config, "causal_peak_capture_min_giveback_r", 0.50),
            peak_r * getattr(self.config, "causal_peak_capture_giveback_pct", 0.25),
        )
        if giveback_r < giveback_threshold:
            return False, giveback_r

        return True, giveback_r

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

    def revert_scale_out(self) -> None:
        """Undo decision-time scale-out flags after a failed exit execution.

        PARTIAL_EXIT sets `_has_scaled_out`/`_runner_qualified` at DECISION time.
        If the broker rejects or loses that order, the brain must not keep
        phantom runner posture (ratcheted floors, ceiling-harvest arming) built
        on a partial exit that never filled. Flags re-qualify naturally on a
        later bar if the setup genuinely deserves them.
        """
        with self._lock:
            if self._is_closed:
                return
            self._has_scaled_out = False
            self._runner_qualified = False


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
    def last_decision(self) -> PISDecision | None:
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
        """
        return self._observe_internal(obs)

    def evaluate(self, obs: PositionObservation) -> PISDecision:
        """Alias for observe(obs) to support evaluation pipelines."""
        return self.observe(obs)

    def _observe_internal(self, obs: PositionObservation) -> PISDecision:
        """Internal observation processing with lock and validation."""
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

        # 2. Market Regime Classification (Quadrant 1)
        regime, regime_conf, regime_summary = self.authority.regime_classifier.classify_regime(
            obs, self._history, health
        )

        # 3. Thesis drift evaluation with Asymmetric Early Invalidation (Quadrant 2)
        thesis = self.authority.thesis_evaluator.evaluate(
            obs, trough_r, self._stagnation_counter, peak_r, history_len=len(self._history)
        )

        # 4. Runner intelligence & Regime-Aware Max R capacity prediction (Quadrant 3)
        predicted_max_r, runner_prob = self.authority.runner_intelligence.predict_max_r(
            obs, peak_r, health, regime=regime
        )
        runner_score, is_runner = self.authority.runner_intelligence.evaluate_runner_score(
            obs, peak_r, health, regime=regime
        )
        if is_runner:
            self._runner_qualified = True

        # 5. State transition rules with persistence confirmation (Priority 3)
        self._update_lifecycle(health, thesis, peak_r)

        # Causal peak capture is evaluated before protection and exit synthesis,
        # using only the completed observations already present in history.
        causal_reversal_confirmed, causal_giveback_r = self._causal_peak_capture_signal(
            obs,
            peak_r,
        )

        # 6. Protection floor calculation with dynamic ceiling proximity (Priority 4)
        target_stop = self.authority.protection_planner.compute_protection_floor(
            obs=obs,
            peak_r=peak_r,
            health=health,
            previous_floor=self._current_stop_floor,
            allow_relaxation=False,
            has_scaled_out=self._has_scaled_out,
            predicted_max_r=predicted_max_r,
            causal_reversal_confirmed=causal_reversal_confirmed,
        )
        self._current_stop_floor = target_stop

        # 7. Exit planner synthesis with early scratch exits and dynamic ceiling harvesting (Quadrant 2 & 3)
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
            causal_reversal_confirmed=causal_reversal_confirmed,
            causal_giveback_r=causal_giveback_r,
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
                causal_reversal_confirmed=causal_reversal_confirmed,
            )
            self._current_stop_floor = target_stop

        # 8. Autonomous Trade Brainstorming & Reasoning Telemetry (Quadrant 4)
        narrative = self.authority.reasoning_engine.generate_narrative(
            obs=obs,
            lifecycle=self.state_machine.current_state,
            health=health,
            thesis=thesis,
            regime=regime,
            regime_summary=regime_summary,
            predicted_max_r=predicted_max_r,
            runner_probability=runner_prob,
            target_stop=target_stop,
            peak_r=peak_r,
            has_scaled_out=self._has_scaled_out,
            action=decision.action,
            reason_codes=decision.reason_codes,
        )

        # Inject regime and narrative into immutable PISDecision
        decision = PISDecision(
            timestamp=decision.timestamp,
            symbol=decision.symbol,
            action=decision.action,
            # PARTIAL_EXIT switches the protection planner into runner mode
            # above.  Expose that recomputed floor in the immutable decision so
            # callers cannot submit/use a stale pre-runner stop.
            target_stop_loss=target_stop,
            exit_ratio=decision.exit_ratio,
            reason_codes=decision.reason_codes,
            health=decision.health,
            thesis=decision.thesis,
            runner_score=decision.runner_score,
            confidence=decision.confidence,
            lifecycle_state=decision.lifecycle_state,
            evidence_coverage=decision.evidence_coverage,
            peak_r=decision.peak_r,
            trough_r=decision.trough_r,
            predicted_max_r=predicted_max_r,
            runner_probability=runner_prob,
            market_regime=regime,
            brainstorm_narrative=narrative,
            metadata={
                **decision.metadata,
                "market_regime": regime.value,
                "regime_summary": regime_summary,
                "brainstorm_narrative": narrative,
            },
        )

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
            elif health.overall < self.config.health_accelerating_threshold and peak_r >= self.config.activation_mfe_r:
                # Reached activation R but momentum cooled: a mature hold, not a
                # fresh accelerator. Previously unreachable state.
                target = LifecycleState.MATURING
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

    def serialize_state(self) -> dict[str, Any]:
        """Serialize state for recovery (Priority 15)."""
        with self._lock:
            return {
                "symbol": self.symbol,
                "lifecycle": self.lifecycle.value,
                "peak_r": self.peak_r,
                "trough_r": self.trough_r,
                "mfe_price": self._profit_memory.mfe_price,
                "mae_price": self._profit_memory.mae_price,
                "current_stop_floor": self._current_stop_floor,
                "has_scaled_out": self._has_scaled_out,
                "runner_qualified": self._runner_qualified,
                "stagnation_counter": self._stagnation_counter,
                "degradation_counter": self._degradation_counter,
                "smoothed_health_score": self._smoothed_health_score,
                "is_closed": self._is_closed,
                "entry_price": self._last_observation.entry_price,
                "side": self._last_observation.side,
                "initial_stop": self._last_observation.initial_stop,
                "last_timestamp": self._last_timestamp.isoformat(),
            }
