"""PIS Exit Planner & Decision Synthesizer.

Synthesizes health, thesis drift, lifecycle state, and protection levels
to produce deterministic, explainable PISDecision outputs.
"""

from __future__ import annotations

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


def is_degraded_thesis(thesis: ThesisDrift) -> bool:
    """True when the trade thesis itself is breaking down (not just price noise)."""
    return bool(getattr(thesis, "is_degraded", False))


class ExitPlanner:
    """Synthesizes position multi-factor state into a PISDecision."""

    def __init__(self, config: PISConfig | None = None) -> None:
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
            if side > 0 and price <= target_stop or side < 0 and price >= target_stop:
                protection_breached = True

        # Check protection floor ratchet update
        protection_updated = False
        if obs.current_stop is not None and target_stop > 0:
            if side > 0 and target_stop > obs.current_stop + 1e-4 or side < 0 and target_stop < obs.current_stop - 1e-4:
                protection_updated = True
        elif target_stop > 0 and abs(target_stop - obs.initial_stop) > 1e-4:
            protection_updated = True

        # Check profit target breach (with near-target climax tolerance when peak_r >= 1.50R)
        target_hit = False
        if obs.take_profit is not None and obs.take_profit > 0:
            target_dist = (obs.take_profit - obs.high) if side > 0 else (obs.low - obs.take_profit)
            target_tolerance = max(0.05, 0.20 * obs.atr)
            if side > 0 and (obs.high >= obs.take_profit or (peak_r >= 1.50 and target_dist <= target_tolerance)) or side < 0 and (obs.low <= obs.take_profit or (peak_r >= 1.50 and target_dist <= target_tolerance)):
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
        early_invalidation_breach = thesis.early_invalidation_detected

        # Check Extended Stagnation / Time-Decay Profit Harvesting
        # 1. Fast post-peak stall: >= 1.0R peak that stalls for >= 10 bars with fading momentum
        # 2. Structural stall over substantial duration (>= 25 bars)
        fast_stagnation = (
            peak_r >= 1.00
            and history_len >= 12
            and thesis.stagnation_bars >= 10
            and health.momentum_score <= 0.35
            and obs.r_multiple >= 0.20
        )
        stagnation_harvest = (
            not has_scaled_out
            and (
                fast_stagnation
                or (
                    history_len >= 25
                    and thesis.stagnation_bars >= 20
                    and peak_r >= 0.50
                    and obs.r_multiple >= 0.30
                    and health.momentum_score <= 0.30
                )
            )
        )

        # --- TWO-STAGE 50/50 INSTITUTIONAL SCALE-OUT & RUNNER EXTENSION ---
        # Stage 1: When target hit, peak R >= effective scale-out target, or
        # genuine exhaustion climax, bank profit.
        # Stage 2: Qualify remaining as runner with breakeven floor + adaptive ATR trail.
        #
        # The scale-out gate is trend-quality adaptive: in a weakening trend
        # (momentum fading or health deteriorating) waiting for the full
        # scale_out_target_r is how winners round-trip, so the gate drops to
        # weak_trend_scale_out_r. Strong trends keep the full target and let
        # runners breathe.
        effective_scale_out_r = self.config.scale_out_target_r
        trend_weakening = (
            health.momentum_score < self.config.weak_momentum_threshold
            or health.overall < self.config.health_weakening_threshold
        )
        if trend_weakening:
            effective_scale_out_r = min(effective_scale_out_r, self.config.weak_trend_scale_out_r)

        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-11 - Predicted-Ceiling Scale-Out Extension
        # ORIGINAL INTENT: Scale-out triggered only at fixed 2.0R (or 1.0R in weak trend),
        # target hit, exhaustion climax, or stagnation harvest.
        # REASON FOR CHANGE: Average winner peak R is ~0.44-1.5R, so fixed 2.0R rarely arms.
        # When predicted_max_r is available, trigger Stage-1 50% scale-out when position reaches
        # >= 70% of predicted ceiling (min 1.0R) with fading momentum (score <= 0.45), banking
        # profit at predicted capacity.
        # SAFE ROLLBACK: The original code is preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # scale_out_triggered = False
        # if getattr(self.config, "enable_scale_out", True) and not has_scaled_out:
        #     if target_hit or peak_r >= effective_scale_out_r or exhaustion_climax or stagnation_harvest:
        #         scale_out_triggered = True
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        predicted_ceiling_scale_out = (
            predicted_max_r > 0
            and peak_r >= max(1.0, 0.70 * predicted_max_r)
            and health.momentum_score <= 0.45
        )
        scale_out_triggered = False
        if getattr(self.config, "enable_scale_out", True) and not has_scaled_out:
            if (
                target_hit
                or peak_r >= effective_scale_out_r
                or exhaustion_climax
                or stagnation_harvest
                or predicted_ceiling_scale_out
            ):
                scale_out_triggered = True
        # =============================================================================

        if scale_out_triggered and not is_runner:
            is_runner = True

        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-12 & CR-13 - Tiered Giveback Guard & DOA Stagnation Cut
        # ORIGINAL INTENT: Flat 55% giveback guard for all peak >= 0.60R; stagnation harvest only on green trades.
        # REASON FOR CHANGE: Excursions > 1R gave back 55% of gains. Tier giveback (40% for 1-2R, 30% for >=2R)
        # to lock in proven moves. Dead-money losers (r <= -0.25 after 12-15 bars with peak < 0.30R)
        # bled 30 bars to full -1R; DOA stagnation cut exits early to protect capital.
        # SAFE ROLLBACK: The original code is preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # giveback_guard_triggered = False
        # if (
        #     getattr(self.config, "enable_profit_giveback_guard", True)
        #     and not has_scaled_out
        #     and not scale_out_triggered
        #     and peak_r >= self.config.profit_guard_peak_r
        #     and obs.r_multiple > 0.0
        # ):
        #     giveback = peak_r - obs.r_multiple
        #     atr_r_buffer = (
        #         getattr(self.config, "profit_guard_min_giveback_atr_r", 0.0)
        #         * (obs.atr / obs.risk_per_share if obs.risk_per_share > 1e-8 else 0.0)
        #     )
        #     min_giveback = min(
        #         peak_r * 0.50,
        #         max(
        #             self.config.profit_guard_max_giveback_pct * peak_r,
        #             atr_r_buffer,
        #         ),
        #     )
        #     if giveback >= min_giveback:
        #         giveback_guard_triggered = True
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        # CR-13: DOA-stall harvest for underwater, zero-excursion dead money
        doa_stall_triggered = False
        if getattr(self.config, "enable_doa_stagnation_harvest", True):
            doa_bars = getattr(self.config, "doa_stagnation_bars", 12)
            doa_r_thresh = getattr(self.config, "doa_r_threshold", -0.25)
            if history_len >= 15 and thesis.stagnation_bars >= doa_bars and peak_r < 0.30 and obs.r_multiple <= doa_r_thresh:
                doa_stall_triggered = True

        giveback_guard_triggered = False
        if (
            getattr(self.config, "enable_profit_giveback_guard", True)
            and not has_scaled_out
            and not scale_out_triggered
            and peak_r >= self.config.profit_guard_peak_r
            and obs.r_multiple > 0.0
        ):
            giveback = peak_r - obs.r_multiple
            atr_r_buffer = (
                getattr(self.config, "profit_guard_min_giveback_atr_r", 0.0)
                * (obs.atr / obs.risk_per_share if obs.risk_per_share > 1e-8 else 0.0)
            )
            # CR-12 Tiered giveback threshold
            if peak_r >= 2.0:
                giveback_pct = getattr(self.config, "profit_guard_pct_tier3", 0.30)
            elif peak_r >= 1.0:
                giveback_pct = getattr(self.config, "profit_guard_pct_tier2", 0.40)
            else:
                giveback_pct = getattr(self.config, "profit_guard_pct_tier1", self.config.profit_guard_max_giveback_pct)

            min_giveback = min(
                peak_r * 0.50,
                max(
                    giveback_pct * peak_r,
                    atr_r_buffer,
                ),
            )
            if giveback >= min_giveback:
                giveback_guard_triggered = True
        # =============================================================================

        # Determine exit action
        # Floor-tag runner gate: thesis must be intact and health not collapsing.
        # A tagged floor WITH degradation is a real exit, not an option to run.
        floor_tag_runner = (
            protection_breached
            and getattr(self.config, "enable_floor_tag_runner", True)
            and not early_invalidation_breach
            and lifecycle != LifecycleState.EXIT_CANDIDATE
            and health.overall >= self.config.health_weakening_threshold
            and not is_degraded_thesis(thesis)
        )

        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-13 - Action determination with DOA stall harvest
        # ORIGINAL INTENT: Early invalidation was the only negative-R fast harvest.
        # REASON FOR CHANGE: Dead-money trades stuck at <= -0.25R with zero excursion for 12+ bars
        # held all the way to 30 bars or -1R. Trigger FULL_EXIT on doa_stall_triggered.
        # SAFE ROLLBACK: The original code is preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if early_invalidation_breach:
        #     action = ExitAction.FULL_EXIT
        #     exit_ratio = 1.0
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if early_invalidation_breach or doa_stall_triggered:
            action = ExitAction.FULL_EXIT
            exit_ratio = 1.0
        # =============================================================================
        elif protection_breached or lifecycle == LifecycleState.EXIT_CANDIDATE:
            if floor_tag_runner and not has_scaled_out:
                # Evidence-driven refinement: the floor was tagged but the trade
                # thesis still holds. Bank half now; the remainder keeps this
                # same floor as its stop, so downside risk shrinks while the
                # measured post-exit continuation gets a vehicle. A second tag
                # (or any degradation) exits everything via has_scaled_out.
                action = ExitAction.PARTIAL_EXIT
                exit_ratio = getattr(self.config, "floor_tag_exit_ratio", 0.50)
            else:
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
        elif giveback_guard_triggered:
            # Bank partial profit into demonstrated weakness while still green.
            action = ExitAction.PARTIAL_EXIT
            exit_ratio = self.config.profit_guard_exit_ratio
        elif (
            lifecycle == LifecycleState.DISTRIBUTION
            and health.overall < self.config.health_distribution_threshold
            and obs.r_multiple >= 0.0
        ):
            # Distribution trim only applies to PROFITABLE positions. Trimming a
            # losing position "because distribution" is a panic exit with extra
            # steps -- the protection floor already governs losses.
            action = ExitAction.PARTIAL_EXIT
            exit_ratio = getattr(self.config, "scale_out_ratio", 0.50)
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

        if early_invalidation_breach:
            reasons.append(ReasonCode.EARLY_THESIS_INVALIDATED)
        elif doa_stall_triggered:
            reasons.append(ReasonCode.DOA_STAGNATION_HARVEST)
        elif floor_tag_runner and action == ExitAction.PARTIAL_EXIT:
            # Order matters: this must precede the plain PROTECTION_BREACH tag so
            # telemetry distinguishes "runner left behind" from "full cut".
            reasons.append(ReasonCode.FLOOR_TAG_RUNNER)
        elif protection_breached:
            reasons.append(ReasonCode.PROTECTION_BREACH)
        elif ceiling_harvested:
            reasons.append(ReasonCode.MAX_R_CEILING_REACHED)
        elif giveback_guard_triggered:
            reasons.append(ReasonCode.PROFIT_GIVEBACK_GUARD)
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
