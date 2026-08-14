"""PIS Runner Intelligence Engine.

Scores runner potential [0.0, 1.0] and determines runner qualification.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from app.pis.models import MarketRegime, PISConfig, PositionObservation, TradeHealth


class RunnerIntelligence:
    """Evaluates runner potential, predicts dynamic Max R capacity, and qualifies runners."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()

    def predict_max_r(
        self,
        obs: PositionObservation,
        peak_r: float,
        health: TradeHealth,
        regime: MarketRegime = MarketRegime.TREND_RUNNER,
    ) -> Tuple[float, float]:
        """Predict dynamic maximum R reachable (R_hat_max) and runner probability.

        Args:
            obs: PositionObservation snapshot.
            peak_r: Peak R achieved so far.
            health: TradeHealth breakdown.
            regime: Active MarketRegime classification.

        Returns:
            Tuple of (predicted_max_r: float, runner_probability: float).
        """
        if not getattr(self.config, "enable_dynamic_max_r", True):
            return 2.0, 0.0
        risk = obs.risk_per_share
        native_target_r = 0.0
        if obs.take_profit is not None and obs.take_profit > 0 and risk > 1e-6:
            native_target_r = abs(obs.take_profit - obs.entry_price) / risk
        r_base = max(1.5, native_target_r)

        health_factor = (
            0.40 * health.overall
            + 0.25 * health.trend_score
            + 0.20 * health.momentum_score
            + 0.15 * health.volume_score
        )
        health_factor = max(0.0, min(1.0, health_factor))
        runner_prob = health_factor

        if regime == MarketRegime.TREND_RUNNER:
            predicted_max_r = r_base * (1.0 + 1.5 * runner_prob)
        elif regime == MarketRegime.VOLATILITY_EXPANSION:
            predicted_max_r = r_base * (1.0 + 1.0 * runner_prob)
        elif regime == MarketRegime.RANGE_BOUND:
            predicted_max_r = min(r_base, 1.5)
        elif regime == MarketRegime.CHOPPY_DEFENSIVE:
            predicted_max_r = min(r_base, 1.25)
        else:
            predicted_max_r = r_base

        if predicted_max_r < peak_r:
            predicted_max_r = max(peak_r * 1.5, predicted_max_r)
        
        # Conservative cap: weak health (overall < 0.50) should not receive bullish ceiling projection
        if health.overall < 0.50:
            predicted_max_r = min(predicted_max_r, r_base)
        
        predicted_max_r = round(predicted_max_r, 2)

        return predicted_max_r, round(runner_prob, 3)

    def evaluate_runner_score(
        self,
        obs: PositionObservation,
        peak_r: float,
        health: TradeHealth,
        regime: MarketRegime = MarketRegime.TREND_RUNNER,
    ) -> Tuple[float, bool]:
        """Evaluate runner score [0.0, 1.0] and qualification status.

        Args:
            obs: PositionObservation.
            peak_r: Peak R achieved.
            health: TradeHealth.
            regime: Active MarketRegime.

        Returns:
            Tuple of (runner_score: float, is_qualified_runner: bool).
        """
        predicted_max_r, runner_prob = self.predict_max_r(obs, peak_r, health, regime=regime)

        target_r = getattr(self.config, "runner_activation_r", 1.0)
        if target_r <= 0.0 or math.isnan(target_r):
            target_r = 1.0

        r_progress = max(0.0, min(1.0, peak_r / target_r)) if peak_r > 0 else 0.0
        health_contrib = health.overall

        runner_score = max(0.0, min(1.0, (r_progress * 0.50) + (health_contrib * 0.25) + (runner_prob * 0.25)))

        runner_active = (
            peak_r >= getattr(self.config, "runner_activation_r", 1.0)
            and runner_prob >= 0.60
            and health.overall >= 0.60
        )

        extended_runner = (
            peak_r >= getattr(self.config, "extended_runner_r", 2.0)
            and health.overall >= 0.55
        )

        is_runner = runner_active or extended_runner

        return runner_score, is_runner
