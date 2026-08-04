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
    ) -> float:
        """Compute the updated protection floor for a position.

        Args:
            obs: Point-in-time PositionObservation.
            peak_r: Peak R-multiple achieved during position life.
            previous_floor: Existing protective stop floor price, if any.
            allow_relaxation: If True, bypasses strict monotonicity (default False).

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

        if risk <= 1e-8 or math.isnan(risk) or math.isinf(risk):
            # Fallback to initial stop if risk is invalid
            return initial_stop

        # 1. Base activation threshold check
        activation_r = min(1.0, max(0.75, self.config.activation_mfe_r * 0.60))

        if peak_r < activation_r:
            # Below activation threshold: keep initial stop or previous floor
            candidate_floor = previous_floor if previous_floor is not None else initial_stop
            
            # Early Bailout Logic: Reduce loss without panic selling
            if getattr(self.config, "enable_early_bailout", True):
                # If health collapses or thesis is degraded, activate Bailout Mode natively
                if health.overall < 0.3 or health.trend_score < 0.25:
                    # Bailout Mode: tighten stop to ride any bounce but cut if dumping resumes
                    bailout_buffer = max(obs.atr * 0.5, obs.close * 0.005)
                    if side > 0:
                        bailout_floor = obs.close - bailout_buffer
                        # Enforce monotonicity: never loosen the stop
                        candidate_floor = max(candidate_floor, bailout_floor)
                    else:
                        bailout_floor = obs.close + bailout_buffer
                        candidate_floor = min(candidate_floor, bailout_floor)
        else:
            # 2. Dynamic giveback calculation based on peak R
            if peak_r >= self.config.runner_qualification_r:
                r_excess = peak_r - self.config.runner_qualification_r
                
                # Health-Aware Decay:
                # If health is perfect (1.0), health_factor is 0.0 -> no decay (survive pullbacks).
                # If health is broken (0.0), health_factor is 1.0 -> max decay (lock profit).
                health_factor = max(0.0, min(1.0, 1.0 - health.overall))
                decay = r_excess * getattr(self.config, "hyper_runner_decay_rate", 0.05) * health_factor
                
                base_giveback = self.config.giveback_pct_runner
                min_giveback = getattr(self.config, "giveback_pct_hyper_runner_min", 0.10)
                
                if getattr(self.config, "profit_harvesting_mode", False) and health.overall >= 0.8:
                    giveback_pct = 0.45  # Let it breathe heavily to capture 25% runs
                else:
                    giveback_pct = max(min_giveback, base_giveback - decay)
            else:
                # Dynamic Exhaustion Prediction: Health-Aware Profit Locking
                # As peak_r expands (>1.0R), progressively reduce giveback allowance to lock high R gains
                r_scaling = max(0.0, (peak_r - 1.0) * 0.15)
                base = max(0.12, self.config.giveback_pct_maturing - r_scaling)
                
                # 1. Parabolic Climax Override (Tighten stop aggressively when extended)
                if peak_r >= 1.50 and health.overall < 0.65:
                    giveback_pct = max(0.10, base * 0.70)  # Lock 90% of peak gain on exhaustion!
                elif health.volatility_score <= 0.2 and peak_r > 1.5:
                    giveback_pct = min(base, 0.12)
                # 2. Healthy Trend (Breathe)
                elif health.overall >= 0.8:
                    giveback_pct = min(0.35, max(base, 0.25))
                # 3. Exhaustion Top / Rejection (Choke)
                elif health.overall <= 0.4:
                    giveback_pct = min(base, 0.15)
                # 4. Smooth Interpolation
                else:
                    giveback_pct = max(0.12, base)

            atr_r_buffer = 0.45 * (obs.atr / max(1e-6, risk))
            giveback_allowance_r = max(self.config.min_breathing_room_r, atr_r_buffer, peak_r * giveback_pct)
            locked_r = max(self.config.min_locked_r, peak_r - giveback_allowance_r)

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
