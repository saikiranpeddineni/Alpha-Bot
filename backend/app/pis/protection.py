"""Adaptive Protection Planner for PIS.

Calculates dynamic, monotonic protective stops that lock in profits
while preserving breathing room based on Peak R, volatility (ATR), and market structure.

Monotonic Protection Invariant:
    For Long (side = 1.0): new_floor >= previous_floor
    For Short (side = -1.0): new_floor <= previous_floor
"""

from __future__ import annotations

import math

from app.pis.models import PISConfig, PositionObservation, TradeHealth


class MonotonicProtectionViolationError(ValueError):
    """Raised when a candidate protective stop weakens the existing protection floor."""

    pass


class AdaptiveProtectionPlanner:
    """Calculates adaptive protection floors satisfying strict monotonicity invariants."""

    def __init__(self, config: PISConfig | None = None) -> None:
        self.config = config or PISConfig()

    def compute_protection_floor(
        self,
        obs: PositionObservation,
        peak_r: float,
        health: TradeHealth,
        previous_floor: float | None = None,
        allow_relaxation: bool = False,
        has_scaled_out: bool = False,
        predicted_max_r: float = 2.0,
        causal_reversal_confirmed: bool = False,
        causal_floor_tightened: bool | None = None,
    ) -> float:
        """Compute the updated protection floor for a position.

        Args:
            obs: Point-in-time PositionObservation.
            peak_r: Peak R-multiple achieved during position life.
            previous_floor: Existing protective stop floor price, if any.
            allow_relaxation: If True, bypasses strict monotonicity (default False).
            has_scaled_out: If True, position has locked initial profit; uses breakeven + adaptive ATR trail.
            predicted_max_r: Predicted dynamic maximum R capacity.
            causal_reversal_confirmed: True only when the position brain has
                confirmed a reversal across completed bars with sufficient
                high-water giveback.  This tightens a runner floor without
                weakening the monotonic invariant.
            causal_floor_tightened: True when either the confirmed reversal or
                the first-bar causal runner ratchet has tightened the floor.

        Returns:
            Calculated stop loss price (float).

        Raises:
            MonotonicProtectionViolationError: If output violates monotonic floor invariant.
            ValueError: If input calculations produce non-finite values.
        """
        entry = obs.entry_price
        risk = obs.risk_per_share
        side = obs.side
        initial_stop = obs.initial_stop
        if causal_floor_tightened is None:
            causal_floor_tightened = causal_reversal_confirmed

        if risk <= 1e-8 or math.isnan(risk) or math.isinf(risk):
            # Fallback to initial stop if risk is invalid
            return initial_stop

        # 1. Base activation threshold check
        activation_r = self.config.activation_mfe_r

        # Dynamic ceiling proximity ratio [0.0, 1.0+]
        proximity = peak_r / max(1.0, predicted_max_r) if predicted_max_r > 0 else 0.50

        # 0. Scaled-Out Runner Mode
        if has_scaled_out:
            breakeven_floor = entry + (side * max(self.config.min_locked_r, 0.05) * risk)

            # Dynamic trailing multiplier based on predicted ceiling proximity
            if proximity >= 0.85:
                # In target ceiling zone: tighten trail to 1.0x ATR to harvest peak R
                trail_mult = min(1.2, getattr(self.config, "runner_wide_atr_mult", 2.0) * 0.60)
            elif proximity >= 0.65:
                trail_mult = getattr(self.config, "runner_wide_atr_mult", 2.0) * 0.80
            else:
                # Early runner phase: allow maximum breathing room (2.0x ATR)
                trail_mult = getattr(self.config, "runner_wide_atr_mult", 2.0)

            # A completed bar's favorable extreme is the only price input used to
            # advance runner protection.  Using close here leaves a large amount of
            # captured excursion unprotected whenever a bar closes back from its
            # high (or low for a short).  The remembered peak is also a completed
            # observation, so it can extend the anchor without lookahead.  Once a
            # bar has already breached the prior floor, do not ratchet from that
            # older peak; monotonicity should preserve the existing floor only.
            peak_price = entry + (side * max(0.0, peak_r) * risk)
            floor_not_breached = (
                previous_floor is None
                or (
                    side > 0
                    and obs.current_price > previous_floor
                    and obs.close > previous_floor
                )
                or (
                    side < 0
                    and obs.current_price < previous_floor
                    and obs.close < previous_floor
                )
            )
            if previous_floor is not None and not floor_not_breached:
                # A breached broker floor is already actionable; never move it
                # again from a reversal bar after the breach.
                candidate_floor = previous_floor
            else:
                if side > 0:
                    runner_anchor = max(obs.high, peak_price)
                else:
                    runner_anchor = min(obs.low, peak_price)
                wide_trail = runner_anchor - (side * trail_mult * obs.atr)
                candidate_floor = max(breakeven_floor, wide_trail) if side > 0 else min(breakeven_floor, wide_trail)

                # Once a completed-bar reversal is confirmed, replace the
                # runner's deliberately wide breathing room with a causal
                # high-water giveback floor.  The floor is capped at the
                # completed-bar close so this path does not use a fresher quote
                # as a hidden future extreme; the final monotonic guard still
                # wins if the hard floor has already been breached.
                if causal_floor_tightened and getattr(self.config, "enable_causal_peak_capture", True):
                    giveback_r = max(
                        getattr(self.config, "causal_peak_capture_min_giveback_r", 0.50),
                        max(0.0, peak_r)
                        * getattr(self.config, "causal_peak_capture_giveback_pct", 0.25),
                    )
                    locked_r = max(
                        self.config.min_locked_r,
                        max(0.0, peak_r) - giveback_r,
                    )
                    causal_floor = entry + (side * locked_r * risk)
                    if side > 0:
                        if causal_reversal_confirmed:
                            causal_floor = min(causal_floor, obs.close)
                        else:
                            buffer = max(
                                self.config.min_price_buffer_abs,
                                obs.close * self.config.price_buffer_pct,
                            )
                            causal_floor = min(causal_floor, obs.close - buffer)
                        candidate_floor = max(candidate_floor, causal_floor)
                    else:
                        if causal_reversal_confirmed:
                            causal_floor = max(causal_floor, obs.close)
                        else:
                            buffer = max(
                                self.config.min_price_buffer_abs,
                                obs.close * self.config.price_buffer_pct,
                            )
                            causal_floor = max(causal_floor, obs.close + buffer)
                        candidate_floor = min(candidate_floor, causal_floor)
        elif peak_r < activation_r:
            # =============================================================================
            # [MODIFIED 2026-10-14]: CR-10 - Adaptive Protection Arming (Strategy-Aware ATR Multiplier)
            # ORIGINAL INTENT: Used flat breakeven_trigger_atr_mult (2.50) for all trades.
            # REASON FOR CHANGE: Breakouts (momentum, orb, bb_squeeze) reverse fast and need earlier
            # protection arming (1.5x ATR) than mean-reversion setups (2.5x ATR).
            # SAFE ROLLBACK: The original code is removed; rollback via git history.
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            atr_r = obs.atr / risk if risk > 1e-8 else 0.0
            cap_trig = getattr(self.config, "progressive_lock_cap_trigger_r", 0.85)
            strat_name = str(getattr(obs, "strategy", "") or "").lower()
            if any(s in strat_name for s in ("reversion", "sweep", "pullback")):
                atr_mult = getattr(self.config, "breakeven_atr_mult_reversion", 2.50)
            elif any(s in strat_name for s in ("momentum", "orb", "squeeze", "breakout", "gap")):
                atr_mult = getattr(self.config, "breakeven_atr_mult_breakout", 1.50)
            else:
                atr_mult = getattr(self.config, "breakeven_trigger_atr_mult", 2.50)

            effective_trigger = min(
                cap_trig,
                max(
                    self.config.breakeven_trigger_r,
                    atr_mult * atr_r,
                ),
            )
            # =============================================================================
            if peak_r >= effective_trigger:
                # Progressive profit lock: once a trade has shown real
                # excursion, stop risking initial capital. Blend from
                # breakeven+buffer at the trigger toward locking a fraction of
                # the peak as it matures toward activation_mfe_r. The floor is
                # derived from peak_r (excursion memory), never the current
                # print, so it cannot oscillate with noise.
                base = max(effective_trigger, 1e-6)
                progress = min(1.0, max(0.0, (peak_r - effective_trigger) / max(activation_r - base, 1e-6)))
                lock_frac = (
                    self.config.progressive_lock_start_frac
                    + (self.config.progressive_lock_end_frac - self.config.progressive_lock_start_frac) * progress
                )
                frac_target = peak_r * lock_frac
                # Never trail inside the noise band: the locked floor must sit
                # at least trail_mult * ATR behind the peak. On quiet names this
                # is slack (frac target binds); on noisy names it forces patience.
                trail_cap = peak_r - getattr(self.config, "progressive_lock_trail_atr_mult", 1.0) * atr_r
                locked_r = min(frac_target, max(self.config.min_locked_r, trail_cap))
                if peak_r >= 1.00:
                    locked_r = max(locked_r, self.config.min_locked_r, 0.05)
                candidate_floor = entry + side * locked_r * risk
            else:
                # Below breakeven trigger: cleanly maintain initial stop loss or previous floor
                # Preserves initial risk geometry and prevents sub-noise shakeouts
                candidate_floor = previous_floor if previous_floor is not None else initial_stop
        else:
            # 2. Dynamic giveback calculation based on peak R and predicted capacity
            if peak_r >= self.config.runner_qualification_r or proximity >= 0.75:
                r_excess = peak_r - self.config.runner_qualification_r

                # Health-Aware Decay:
                # If health is perfect (1.0), health_factor is 0.0 -> no decay (survive pullbacks).
                # If health is broken (0.0), health_factor is 1.0 -> max decay (lock profit).
                health_factor = max(0.0, min(1.0, 1.0 - health.overall))
                decay = r_excess * getattr(self.config, "hyper_runner_decay_rate", 0.05) * health_factor

                base_giveback = self.config.giveback_pct_runner
                min_giveback = getattr(self.config, "giveback_pct_hyper_runner_min", 0.10)

                # Proximity compression: If approaching predicted ceiling, tighten giveback
                if proximity >= 0.85:
                    giveback_pct = max(min_giveback, 0.15)
                elif getattr(self.config, "profit_harvesting_mode", False) and health.overall >= 0.8:
                    giveback_pct = 0.45  # Let it breathe heavily to capture extended runs
                else:
                    giveback_pct = max(min_giveback, base_giveback - decay)
            else:
                # Dynamic Exhaustion Prediction: Health-Aware Profit Locking
                # As peak_r expands (>1.0R), progressively reduce giveback allowance to lock high R gains
                r_scaling = max(0.0, (peak_r - 1.0) * 0.15)
                base = max(0.12, self.config.giveback_pct_maturing - r_scaling)

                # 1. Parabolic Climax Override (Tighten stop aggressively when extended)
                if peak_r >= 1.50 and (health.overall < 0.65 or proximity >= 0.85):
                    giveback_pct = max(0.10, base * 0.70)  # Lock 90% of peak gain on exhaustion!
                elif health.volatility_score <= 0.2 and peak_r > 1.5:
                    giveback_pct = min(base, 0.12)
                # 2. Healthy Trend (Breathe)
                elif health.overall >= 0.8:
                    giveback_pct = min(0.35, max(base, 0.25))
                # 3. Exhaustion Top / Rejection (Choke)
                elif health.overall <= 0.4:
                    giveback_pct = min(base, 0.15)
                # 4. Smooth Interpolation with Volume-Weighted Elasticity
                else:
                    # Elastic Trailing Stop:
                    # High volume_score (healthy absorption) relaxes the stop.
                    # Low volume_score (distribution) chokes the stop.
                    elasticity = max(0.5, min(1.5, health.volume_score / 0.80))
                    giveback_pct = max(0.12, base * elasticity)

            runner_threshold = getattr(self.config, "hyper_runner_threshold_r", 1.50)
            if peak_r >= runner_threshold and health.overall >= 0.70 and proximity < 0.80:
                atr_r_buffer = 1.5 * (obs.atr / max(1e-6, risk))
            else:
                atr_r_buffer = 1.0 * (obs.atr / max(1e-6, risk))

            # Scale dynamic breathing room and atr buffer proportionally to Peak R
            # Prevents atr_r_buffer from exceeding 45% of peak_r and draining mature gains
            dynamic_min_breathing = min(self.config.min_breathing_room_r, peak_r * 0.35)
            dynamic_atr_buffer = min(peak_r * 0.45, atr_r_buffer)

            if health.overall < 0.50 or (peak_r >= 1.50 and health.overall < 0.65):
                # Scale down the mandatory buffers so we can choke
                health_factor = max(0.2, health.overall)
                dynamic_min_breathing *= health_factor
                dynamic_atr_buffer *= health_factor

            giveback_allowance_r = max(dynamic_min_breathing, min(dynamic_atr_buffer, peak_r * giveback_pct))
            locked_r = max(self.config.min_locked_r, peak_r - giveback_allowance_r)
            if peak_r >= 1.00:
                locked_r = max(locked_r, self.config.min_locked_r, 0.05)

            # Raw desired floor from R-multiple locking
            desired_floor = entry + side * locked_r * risk

            # 3. Ensure floor does not cross below breakeven once locked
            if side > 0:
                desired_floor = max(desired_floor, entry + (risk * self.config.min_locked_r))
            else:
                desired_floor = min(desired_floor, entry - (risk * self.config.min_locked_r))

            candidate_floor = desired_floor

        # 4. Enforce strict monotonicity against previous floor
        if previous_floor is not None:
            if side > 0:  # Long
                if candidate_floor < previous_floor:
                    if not allow_relaxation:
                        candidate_floor = previous_floor
                    else:
                        raise MonotonicProtectionViolationError(
                            f"Long protection floor weakened: new {candidate_floor} < prev {previous_floor}"
                        )
            else:  # Short
                if candidate_floor > previous_floor:
                    if not allow_relaxation:
                        candidate_floor = previous_floor
                    else:
                        raise MonotonicProtectionViolationError(
                            f"Short protection floor weakened: new {candidate_floor} > prev {previous_floor}"
                        )

        # Final numerical safety check
        if math.isnan(candidate_floor) or math.isinf(candidate_floor) or candidate_floor <= 0.0:
            return previous_floor if previous_floor is not None else initial_stop

        return candidate_floor
