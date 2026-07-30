"""PIS Evidence Gathering & Explainability Engine.

Gather evidence metrics and build structured reason codes.
"""

from __future__ import annotations

from typing import Dict, List

from app.pis.models import (
    EvidenceCoverage,
    LifecycleState,
    PositionObservation,
    ReasonCode,
    ThesisDrift,
    TradeHealth,
)


class EvidenceEvaluator:
    """Computes evidence coverage and explainable reason codes."""

    @staticmethod
    def compute_coverage(
        history_len: int,
        health: TradeHealth,
        thesis: ThesisDrift,
    ) -> EvidenceCoverage:
        """Compute evidence coverage metrics."""
        quality = min(1.0, history_len / 20.0)
        confidence = min(1.0, (quality * 0.4) + (health.overall * 0.6))

        factors = {
            "history_quality": quality,
            "trend_factor": health.trend_score,
            "momentum_factor": health.momentum_score,
            "volatility_factor": health.volatility_score,
            "thesis_drift": thesis.drift_score,
        }

        return EvidenceCoverage(
            bars_observed=history_len,
            data_quality_score=quality,
            confidence=confidence,
            evidence_factors=factors,
        )

    @staticmethod
    def build_reason_codes(
        lifecycle: LifecycleState,
        health: TradeHealth,
        thesis: ThesisDrift,
        protection_updated: bool,
        is_runner: bool,
    ) -> List[ReasonCode]:
        """Construct deterministic set of reason codes."""
        reasons: List[ReasonCode] = []

        if lifecycle == LifecycleState.NEW:
            reasons.append(ReasonCode.INITIAL_ENTRY)

        if is_runner or lifecycle == LifecycleState.RUNNER:
            reasons.append(ReasonCode.RUNNER_QUALIFIED)

        if protection_updated:
            reasons.append(ReasonCode.PROTECTION_RATCHET)

        if health.overall >= 0.70:
            reasons.append(ReasonCode.HEALTHY_TREND)
        elif health.overall <= 0.35:
            reasons.append(ReasonCode.HEALTH_EXHAUSTION)

        if thesis.is_degraded:
            reasons.append(ReasonCode.THESIS_DEGRADED)

        if lifecycle == LifecycleState.DISTRIBUTION:
            reasons.append(ReasonCode.DISTRIBUTION_DETECTED)

        return reasons if reasons else [ReasonCode.HEALTHY_TREND]
