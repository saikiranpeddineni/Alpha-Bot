"""PIS Runner Intelligence Engine.

Scores runner potential [0.0, 1.0] and determines runner qualification.
"""

from __future__ import annotations

import math
from typing import Optional

from app.pis.models import PISConfig, PositionObservation, TradeHealth


class RunnerIntelligence:
    """Evaluates runner potential and qualification."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()

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
        target_r = self.config.runner_qualification_r
        if target_r <= 0.0 or math.isnan(target_r):
            target_r = 3.0

        r_progress = max(0.0, min(1.0, peak_r / target_r)) if peak_r > 0 else 0.0
        health_contrib = health.overall

        runner_score = max(0.0, min(1.0, (r_progress * 0.70) + (health_contrib * 0.30)))

        is_runner = peak_r >= target_r and health.overall >= self.config.health_weakening_threshold

        return runner_score, is_runner
