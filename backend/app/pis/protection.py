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

from app.pis.models import PISConfig, PositionObservation


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
        else:
            # 2. Dynamic giveback calculation based on peak R
            giveback_pct = (
                self.config.giveback_pct_runner
                if peak_r >= self.config.runner_qualification_r
                else self.config.giveback_pct_maturing
            )
            giveback_allowance_r = max(self.config.min_breathing_room_r, peak_r * giveback_pct)
            locked_r = max(self.config.min_locked_r, peak_r - giveback_allowance_r)

            # Raw desired floor from R-multiple locking
            desired_floor = entry + side * locked_r * risk

            # 3. Buffer relative to completed close to prevent noise whip
            px_buffer = max(self.config.min_price_buffer_abs, obs.close * self.config.price_buffer_pct)
            if side > 0:
                desired_floor = min(desired_floor, obs.close - px_buffer)
                # Ensure floor is at least breakeven + minimal locked risk
                desired_floor = max(desired_floor, entry + (risk * self.config.min_locked_r))
            else:
                desired_floor = max(desired_floor, obs.close + px_buffer)
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
