"""PIS Runner Intelligence Engine.

Scores runner potential [0.0, 1.0] and determines runner qualification.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from app.pis.models import PISConfig, PositionObservation, TradeHealth


class RunnerIntelligence:
    """Evaluates runner potential, predicts dynamic Max R capacity, and qualifies runners."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()

    def predict_max_r(
        self,
        obs: PositionObservation,
        peak_r: float,
        health: TradeHealth,
    ) -> Tuple[float, float]:
        """Predict dynamic maximum R reachable (R_hat_max) and runner probability.

        Args:
            obs: PositionObservation snapshot.
            peak_r: Peak R achieved so far.
            health: TradeHealth breakdown.

        Returns:
            Tuple of (predicted_max_r: float, runner_probability: float).
        """
        if not getattr(self.config, "enable_dynamic_max_r", True):
            return 2.0, 0.0

        # 1. Base Target R from trade setup or default baseline (2.0R)
        risk = obs.risk_per_share
        if obs.take_profit is not None and obs.take_profit > 0 and risk > 1e-6:
            native_target_r = abs(obs.take_profit - obs.entry_price) / risk
            r_base = max(2.0, native_target_r)
        else:
            r_base = 2.0

        # 2. Extract multi-dimensional health and order-flow signals
        trend_factor = max(0.0, min(1.0, health.trend_score))
        mom_factor = max(0.0, min(1.0, health.momentum_score))
        vol_factor = max(0.0, min(1.0, health.volume_score))

        # 3. Composite Runner Probability P_runner in [0.0, 1.0]
        runner_prob = (0.40 * trend_factor) + (0.35 * mom_factor) + (0.25 * vol_factor)

        # 4. Dynamic Expansion Multiplier
        # When institutional volume and momentum confirm trend acceleration, expand capacity
        if health.overall >= 0.70 and runner_prob >= 0.60:
            expansion = 1.0 + (runner_prob * 1.50)  # Up to 2.5x expansion of baseline target
            predicted_max_r = r_base * expansion
        elif health.overall >= 0.55:
            expansion = 1.0 + (runner_prob * 0.50)
            predicted_max_r = r_base * expansion
        else:
            predicted_max_r = r_base

        # 5. Peak R Ratchet: If the stock is already running strong, extend ceiling ahead of peak
        if health.overall >= 0.75 and peak_r >= (predicted_max_r * 0.80):
            predicted_max_r = max(predicted_max_r, peak_r + 1.00)

        max_cap = getattr(self.config, "max_r_cap", 8.0)
        predicted_max_r = min(max_cap, round(predicted_max_r, 2))

        return predicted_max_r, round(runner_prob, 3)

    def evaluate_runner_score(
        self,
        obs: PositionObservation,
        peak_r: float,
        health: TradeHealth,
    ) -> Tuple[float, bool]:
        """Evaluate runner score [0.0, 1.0] and qualification status.

        Args:
            obs: PositionObservation.
            peak_r: Peak R achieved.
            health: TradeHealth.

        Returns:
            Tuple of (runner_score: float, is_qualified_runner: bool).
        """
        predicted_max_r, runner_prob = self.predict_max_r(obs, peak_r, health)

        target_r = self.config.runner_qualification_r
        if target_r <= 0.0 or math.isnan(target_r):
            target_r = 3.0

        r_progress = max(0.0, min(1.0, peak_r / target_r)) if peak_r > 0 else 0.0
        health_contrib = health.overall

        runner_score = max(0.0, min(1.0, (r_progress * 0.50) + (health_contrib * 0.25) + (runner_prob * 0.25)))

        is_runner = (peak_r >= target_r or (peak_r >= 1.5 and runner_prob >= 0.75)) and health.overall >= self.config.health_weakening_threshold

        return runner_score, is_runner
