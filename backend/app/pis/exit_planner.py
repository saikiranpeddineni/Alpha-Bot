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

        # Check Exhaustion / Rejection Wick at peak profit (>= 1.10R)
        bar_range = obs.high - obs.low
        exhaustion_wick = False
        if peak_r >= 1.10 and bar_range >= 0.70 * obs.atr:
            adverse_wick = (obs.high - obs.close) if side > 0 else (obs.close - obs.low)
            wick_ratio = adverse_wick / max(1e-6, bar_range)
            if wick_ratio >= 0.45:
                exhaustion_wick = True

        # --- INSTITUTIONAL 85/15 SCALE-OUT & RUNNER EXTENSION ---
        # When target is reached or exhaustion wick forms:
        # Secure 85% core profit and qualify the remaining 15% as an active trailing runner.
        scale_out_triggered = (target_hit or exhaustion_wick)
        if scale_out_triggered and not is_runner:
            is_runner = True

        # Determine exit action
        if protection_breached or lifecycle == LifecycleState.EXIT_CANDIDATE:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        elif scale_out_triggered and is_runner:
            # Institutional Scale-Out: Secure 85% core profit at target/exhaustion, trail 15% runner
            action = ExitAction.PARTIAL_EXIT
            exit_ratio = 0.85
        elif target_hit and not is_runner:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
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
            metadata={
                "is_runner": is_runner,
                "protection_updated": protection_updated,
                "protection_breached": protection_breached,
            },
        )
