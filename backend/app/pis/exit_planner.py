"""PIS Exit Planner & Decision Synthesizer.

Synthesizes health, thesis drift, lifecycle state, and protection levels
to produce deterministic, explainable PISDecision outputs.
"""

from __future__ import annotations

from typing import List, Optional

from app.pis.evidence import EvidenceEvaluator
from app.pis.models import (
    ExitAction,
    LifecycleState,
    PISConfig,
    PISDecision,
    PositionObservation,
    ReasonCode,
    ThesisDrift,
    TradeHealth,
)


class ExitPlanner:
    """Synthesizes position multi-factor state into a PISDecision."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()

    def synthesize_decision(
        self,
        obs: PositionObservation,
        lifecycle: LifecycleState,
        health: TradeHealth,
        thesis: ThesisDrift,
        runner_score: float,
        is_runner: bool,
        target_stop: float,
        peak_r: float,
        trough_r: float,
        history_len: int,
        has_scaled_out: bool = False,
        predicted_max_r: float = 2.0,
        runner_probability: float = 0.0,
        exit_planner_mode: str = "STRICT",
    ) -> PISDecision:
        """Synthesize final PISDecision.

        Args:
            obs: PositionObservation snapshot.
            lifecycle: Current LifecycleState.
            health: TradeHealth breakdown.
            thesis: ThesisDrift breakdown.
            runner_score: Runner potential score [0.0, 1.0].
            is_runner: True if qualified runner.
            target_stop: Protection floor target stop loss price.
            peak_r: Peak R achieved.
            trough_r: Trough R achieved.
            history_len: Count of history observations.
            has_scaled_out: True if position has already executed its first-stage scale out.
            predicted_max_r: Predicted maximum R capacity.
            runner_probability: Quant runner probability score [0.0, 1.0].

        Returns:
            Immutable PISDecision.
        """
        side = obs.side
        price = obs.current_price

        # Check protection breach
        protection_breached = False
        if target_stop > 0:
            if side > 0 and price <= target_stop:
                protection_breached = True
            elif side < 0 and price >= target_stop:
                protection_breached = True

        # Check protection floor ratchet update
        protection_updated = False
        if obs.current_stop is not None and target_stop > 0:
            if side > 0 and target_stop > obs.current_stop + 1e-4:
                protection_updated = True
            elif side < 0 and target_stop < obs.current_stop - 1e-4:
                protection_updated = True
        elif target_stop > 0 and abs(target_stop - obs.initial_stop) > 1e-4:
            protection_updated = True

        # Check profit target breach (with near-target climax tolerance when peak_r >= 1.50R)
        target_hit = False
        if obs.take_profit is not None and obs.take_profit > 0:
            target_dist = (obs.take_profit - obs.high) if side > 0 else (obs.low - obs.take_profit)
            target_tolerance = max(0.05, 0.20 * obs.atr)
            if side > 0 and (obs.high >= obs.take_profit or (peak_r >= 1.50 and target_dist <= target_tolerance)):
                target_hit = True
            elif side < 0 and (obs.low <= obs.take_profit or (peak_r >= 1.50 and target_dist <= target_tolerance)):
                target_hit = True

        # Check Climax Reversal Exhaustion (On extreme extension >= 1.50R with wide-range blowoff wick)
        bar_range = obs.high - obs.low
        exhaustion_climax = False
        if peak_r >= 1.50 and bar_range >= 2.0 * obs.atr:
            adverse_wick = (obs.high - obs.close) if side > 0 else (obs.close - obs.low)
            wick_ratio = adverse_wick / max(1e-6, bar_range)
            if wick_ratio >= 0.60:
                exhaustion_climax = True

        # Check Predicted Max-R Ceiling Exhaustion Harvesting
        # Triggered when position reaches its predicted max R ceiling with signs of momentum exhaustion
        ceiling_harvested = False
        if (
            predicted_max_r > 0
            and peak_r >= (predicted_max_r * 0.95)
            and (health.overall <= 0.60 or health.momentum_score <= 0.35 or exhaustion_climax)
        ):
            ceiling_harvested = True

        # Check Early Thesis Invalidation Scratch Exit (Quadrant 2)
        # In PATIENT mode, we ignore early invalidations for proven trades with intact theses
        if exit_planner_mode == "STRICT":
            early_invalidation_breach = thesis.early_invalidation_detected
        else:
            early_invalidation_breach = False

        # Check Extended Stagnation / Time-Decay Profit Harvesting
        # Requires true structural stall over substantial duration (>= 25 bars) without progress
        stagnation_harvest = (
            not has_scaled_out
            and history_len >= 25
            and thesis.stagnation_bars >= 20
            and peak_r >= 0.50
            and obs.r_multiple >= 0.30
            and health.momentum_score <= 0.30
        )

        # --- TWO-STAGE 50/50 INSTITUTIONAL SCALE-OUT & RUNNER EXTENSION ---
        # Stage 1: When target hit, peak R >= 1.0R, or genuine exhaustion climax, bank 50% profit.
        # Stage 2: Qualify remaining 50% as runner with breakeven floor + adaptive ATR trail.
        scale_out_triggered = False
        if getattr(self.config, "enable_scale_out", True) and not has_scaled_out:
            scale_out_r = getattr(self.config, "scale_out_target_r", 2.00)  # Delayed from 1.0R to 2.0R to maximize expectancy
            if target_hit or peak_r >= scale_out_r or exhaustion_climax or stagnation_harvest:
                scale_out_triggered = True

        if scale_out_triggered and not is_runner:
            is_runner = True

        # Determine exit action
        if protection_breached:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        elif thesis.severe_adverse_override_triggered:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        elif early_invalidation_breach:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        elif lifecycle == LifecycleState.EXIT_CANDIDATE:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        elif ceiling_harvested and has_scaled_out:
            # Full harvest of runner at predicted max R ceiling
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        elif scale_out_triggered:
            # Stage 1 Scale-Out: Secure 50% core profit, trail remaining 50% as runner
            action = ExitAction.PARTIAL_EXIT
            exit_ratio = getattr(self.config, "scale_out_ratio", 0.50)
        elif lifecycle == LifecycleState.DISTRIBUTION and health.overall < self.config.health_distribution_threshold:
            action = ExitAction.PARTIAL_EXIT
            exit_ratio = 0.50
        elif protection_updated:
            action = ExitAction.UPDATE_PROTECTION
            exit_ratio = 0.0
        else:
            action = ExitAction.HOLD
            exit_ratio = 0.0

        # Build evidence coverage and reason codes
        coverage = EvidenceEvaluator.compute_coverage(history_len, health, thesis)
        reasons = EvidenceEvaluator.build_reason_codes(
            lifecycle, health, thesis, protection_updated, is_runner
        )

        if protection_breached:
            reasons.append(ReasonCode.PROTECTION_BREACH)
        elif thesis.severe_adverse_override_triggered:
            reasons.append(ReasonCode.EMERGENCY_STOP)
        elif early_invalidation_breach:
            reasons.append(ReasonCode.EARLY_THESIS_INVALIDATED)
        elif ceiling_harvested:
            reasons.append(ReasonCode.MAX_R_CEILING_REACHED)
        elif scale_out_triggered:
            reasons.append(ReasonCode.SCALE_OUT_TARGET)
        elif target_hit:
            reasons.append(ReasonCode.HEALTHY_TREND)

        return PISDecision(
            timestamp=obs.timestamp,
            symbol=obs.symbol,
            action=action,
            target_stop_loss=target_stop,
            exit_ratio=exit_ratio,
            reason_codes=reasons,
            health=health,
            thesis=thesis,
            runner_score=runner_score,
            confidence=coverage.confidence,
            lifecycle_state=lifecycle,
            evidence_coverage=coverage,
            peak_r=peak_r,
            trough_r=trough_r,
            predicted_max_r=predicted_max_r,
            runner_probability=runner_probability,
            metadata={
                "is_runner": is_runner,
                "protection_updated": protection_updated,
                "protection_breached": protection_breached,
                "predicted_max_r": predicted_max_r,
                "runner_probability": runner_probability,
                "early_invalidation": early_invalidation_breach,
            },
        )
