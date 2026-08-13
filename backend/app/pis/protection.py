"""Adaptive Protection Planner for PIS.

Calculates dynamic, monotonic protective stops that lock in profits
while preserving breathing room based on Peak R, volatility (ATR), and market structure.

Monotonic Protection Invariant:
    For Long (side = 1.0): new_floor >= previous_floor
    For Short (side = -1.0): new_floor <= previous_floor
"""

from __future__ import annotations

import math
from typing import Optional

from app.pis.models import PISConfig, PositionObservation, TradeHealth


class MonotonicProtectionViolationError(ValueError):
    """Raised when a candidate protective stop weakens the existing protection floor."""

    pass


class AdaptiveProtectionPlanner:
    """Calculates adaptive protection floors satisfying strict monotonicity invariants."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()

    def compute_protection_floor(
        self,
        obs: PositionObservation,
        peak_r: float,
        health: TradeHealth,
        previous_floor: Optional[float] = None,
        allow_relaxation: bool = False,
        has_scaled_out: bool = False,
        predicted_max_r: float = 2.0,
    ) -> float:
        """Compute the updated protection floor for a position.

        Args:
            obs: Point-in-time PositionObservation.
            peak_r: Peak R-multiple achieved during position life.
            previous_floor: Existing protective stop floor price, if any.
            allow_relaxation: If True, permit a weaker candidate floor; otherwise enforce monotonicity.
            has_scaled_out: If True, position has locked initial profit; uses breakeven + adaptive ATR trail.
            predicted_max_r: Predicted dynamic maximum R capacity.

        Returns:
            Calculated stop loss price (float).

        Raises:
            MonotonicProtectionViolationError: Reserved for explicit strict-validation callers.
            ValueError: If input calculations produce non-finite values.
        """
        entry = obs.entry_price
        risk = obs.risk_per_share
        side = obs.side
        initial_stop = obs.initial_stop

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

            wide_trail = obs.close - (side * trail_mult * obs.atr)
            candidate_floor = max(breakeven_floor, wide_trail) if side > 0 else min(breakeven_floor, wide_trail)
        elif peak_r < activation_r:
            # Below activation threshold: cleanly maintain initial stop loss or previous floor
            # Preserves initial risk geometry and prevents sub-noise shakeouts
            candidate_floor = previous_floor if previous_floor is not None else initial_stop
            
            # Intermediate Deterioration Protection
            # If a trade establishes early profitability but structurally deteriorates
            # before reaching formal runner activation (1.50R), protect accumulated value dynamically.
            intermediate_peak_threshold = getattr(self.config, "intermediate_protection_min_r", 0.25)
            
            if peak_r >= intermediate_peak_threshold:
                # Assess structural/health deterioration
                r_giveback = peak_r - obs.r_multiple
                is_deteriorating = False
                intermediate_locked_r = -1.0 # fallback
                
                # Severe deterioration: Lock breakeven (+0.05R for slippage/fees)
                if health.overall < 0.40 or r_giveback >= 0.70:
                    intermediate_locked_r = 0.05
                    is_deteriorating = True
                # Moderate deterioration: Halve the risk (move stop to -0.5R)
                elif health.overall < 0.55 or r_giveback >= 0.40:
                    intermediate_locked_r = -0.50
                    is_deteriorating = True
                    
                if is_deteriorating:
                    intermediate_floor = entry + (side * intermediate_locked_r * risk)
                    if side > 0:
                        candidate_floor = max(candidate_floor, intermediate_floor)
                    else:
                        candidate_floor = min(candidate_floor, intermediate_floor)
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
                # 2. Healthy Trend or Pullback (Breathe)
                elif health.overall >= 0.8 or getattr(health, "structure_classification", "") == "HEALTHY_PULLBACK":
                    giveback_pct = min(0.45, max(base * 1.2, 0.30))
                # 3. Exhaustion Top / Rejection / Breakdown (Choke)
                elif health.overall <= 0.4 or getattr(health, "structure_classification", "") == "STRUCTURAL_BREAKDOWN":
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
                atr_r_buffer = 2.0 * (obs.atr / max(1e-6, risk))
            else:
                atr_r_buffer = 1.5 * (obs.atr / max(1e-6, risk))

            # If health is degrading or we are exhausted, we must allow the trailing stop to choke tighter
            # than the min_breathing_room. Otherwise the max() function blinds the PIS exhaustion logic.
            dynamic_min_breathing = self.config.min_breathing_room_r
            dynamic_atr_buffer = atr_r_buffer

            if getattr(health, "structure_classification", "") == "HEALTHY_PULLBACK":
                pass # Maintain wide buffers
            elif health.overall < 0.50 or (peak_r >= 1.50 and health.overall < 0.65) or getattr(health, "structure_classification", "") == "STRUCTURAL_BREAKDOWN":
                # Scale down the mandatory buffers so we can choke
                health_factor = max(0.2, health.overall)
                dynamic_min_breathing *= health_factor
                dynamic_atr_buffer *= health_factor

            giveback_allowance_r = max(dynamic_min_breathing, dynamic_atr_buffer, peak_r * giveback_pct)
            locked_r = max(self.config.min_locked_r, peak_r - giveback_allowance_r)

            # Raw desired floor from R-multiple locking
            desired_floor = entry + side * locked_r * risk

            # 3. Ensure floor does not cross below breakeven once locked
            if side > 0:
                desired_floor = max(desired_floor, entry + (risk * self.config.min_locked_r))
            else:
                desired_floor = min(desired_floor, entry - (risk * self.config.min_locked_r))

            candidate_floor = desired_floor

        # 4. Enforce strict monotonicity unless the caller explicitly permits relaxation.
        if previous_floor is not None:
            if side > 0:  # Long
                if candidate_floor < previous_floor and not allow_relaxation:
                    candidate_floor = previous_floor
            else:  # Short
                if candidate_floor > previous_floor and not allow_relaxation:
                    candidate_floor = previous_floor

        # Final numerical safety check
        if math.isnan(candidate_floor) or math.isinf(candidate_floor) or candidate_floor <= 0.0:
            return previous_floor if previous_floor is not None else initial_stop

        return candidate_floor
