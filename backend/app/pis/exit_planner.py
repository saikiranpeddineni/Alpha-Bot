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

        # Check profit target breach
        target_hit = False
        if obs.take_profit is not None and obs.take_profit > 0:
            if side > 0 and obs.high >= obs.take_profit:
                target_hit = True
            elif side < 0 and obs.low <= obs.take_profit:
                target_hit = True

        # --- DYNAMIC PROFIT EXTENSION ---
        # If the target is hit but the stock is exceptionally strong, we extend the trade
        # by converting it into a runner dynamically.
        target_extended = False
        if target_hit and not is_runner:
            if health.overall >= 0.80 or health.momentum_score >= 0.80 or health.news_sentiment >= 0.70:
                target_extended = True
                target_hit = False  # Override the exit, force HOLD
                is_runner = True    # Upgrade to runner status for future trailing

        # Determine exit action
        if protection_breached or lifecycle == LifecycleState.EXIT_CANDIDATE:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        elif target_hit and not is_runner:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        elif target_hit and is_runner:
            # Let runners run, but secure 50% at target
            action = ExitAction.PARTIAL_EXIT
            exit_ratio = 0.5
        elif lifecycle == LifecycleState.DISTRIBUTION and health.overall < self.config.health_distribution_threshold:
            action = ExitAction.PARTIAL_EXIT
            exit_ratio = 0.5
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
