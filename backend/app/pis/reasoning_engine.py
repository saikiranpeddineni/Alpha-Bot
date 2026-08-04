"""PIS Autonomous Trade Reasoning & Brainstorming Engine (Quadrant 4).

Generates real-time, explainable quantitative trade brainstorming narratives,
synthesizing market regime, risk management boundaries, predicted ceiling capacity,
and order-flow dynamics for every evaluation tick/minute.
"""

from __future__ import annotations

import math
from typing import Optional

from app.pis.models import (
    ExitAction,
    LifecycleState,
    MarketRegime,
    PISConfig,
    PositionObservation,
    ReasonCode,
    ThesisDrift,
    TradeHealth,
)


class TradeReasoningEngine:
    """Synthesizes autonomous trade brainstorming and explainable quantitative reasoning."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()

    def generate_narrative(
        self,
        obs: PositionObservation,
        lifecycle: LifecycleState,
        health: TradeHealth,
        thesis: ThesisDrift,
        regime: MarketRegime,
        regime_summary: str,
        predicted_max_r: float,
        runner_probability: float,
        target_stop: float,
        peak_r: float,
        has_scaled_out: bool,
        action: ExitAction,
        reason_codes: list[ReasonCode],
    ) -> str:
        """Synthesize institutional trade brainstorming narrative.

        Args:
            obs: PositionObservation snapshot.
            lifecycle: Current lifecycle state.
            health: Multi-factor health breakdown.
            thesis: Thesis drift metrics.
            regime: Active MarketRegime.
            regime_summary: Regime classifier explanation.
            predicted_max_r: Quantitative maximum R capacity.
            runner_probability: Probability score of runner expansion [0.0, 1.0].
            target_stop: Protective stop floor price.
            peak_r: Peak R-multiple achieved so far.
            has_scaled_out: True if early tranche has been banked.
            action: Evaluated ExitAction.
            reason_codes: Reason codes triggered.

        Returns:
            Human-readable, quant-backed narrative briefing string.
        """
        if not getattr(self.config, "enable_trade_reasoning_narrative", True):
            return ""

        side_str = "LONG" if obs.side > 0 else "SHORT"
        current_r = obs.r_multiple
        risk = max(1e-6, obs.risk_per_share)

        # 1. Ceiling Proximity & Strategy
        if predicted_max_r > 0:
            proximity_pct = min(100.0, max(0.0, (peak_r / predicted_max_r) * 100.0))
        else:
            proximity_pct = 0.0

        # 2. Risk / Downside Watch Status
        if target_stop > 0:
            stop_dist = abs(obs.current_price - target_stop)
            stop_dist_r = stop_dist / risk
            risk_summary = f"Stop floor @ ${target_stop:.2f} ({stop_dist_r:.2f}R cushion)"
        else:
            risk_summary = f"Initial stop @ ${obs.initial_stop:.2f}"

        # 3. Action Context
        reasons_str = ", ".join(r.value for r in reason_codes) if reason_codes else "NORMAL_MONITORING"

        # 4. Synthesize Component Briefing
        narrative_parts = []
        narrative_parts.append(
            f"[{obs.symbol} {side_str}] Regime: {regime.value} ({regime_summary})."
        )
        narrative_parts.append(
            f"P&L: {current_r:+.2f}R (Peak: {peak_r:+.2f}R, Trough: {thesis.adverse_excursion_r:+.2f}R). "
            f"Health: {health.overall:.2f} (Trend: {health.trend_score:.2f}, Mom: {health.momentum_score:.2f}, Vol: {health.volume_score:.2f})."
        )

        if thesis.early_invalidation_detected:
            narrative_parts.append(
                "🚨 ASYMMETRIC LOSS CUT: Early thesis invalidation triggered. Adverse volume absorption with zero upside traction; scratching trade to preserve capital."
            )
        elif action == ExitAction.FULL_EXIT:
            narrative_parts.append(
                f"🛑 EXIT EXECUTING: {reasons_str}. Position closed to lock gains or prevent further adverse excursion."
            )
        elif action == ExitAction.PARTIAL_EXIT:
            narrative_parts.append(
                f"💰 SCALE-OUT EXECUTING: {reasons_str}. Banked 50% profit foundation; transitioning remaining position to runner."
            )
        else:
            scale_status = "Tranche 1 Banked" if has_scaled_out else "Full Position Open"
            narrative_parts.append(
                f"🎯 TARGET CAPACITY: Predicted Max R = {predicted_max_r:.2f}R (Runner Prob: {runner_probability:.0%}, Ceiling Proximity: {proximity_pct:.1f}%). "
                f"Status: {scale_status}. {risk_summary}."
            )

        return " ".join(narrative_parts)
