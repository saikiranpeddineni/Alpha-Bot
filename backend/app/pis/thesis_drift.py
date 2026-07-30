"""PIS Thesis Drift Tracking Engine.

Measures thesis degradation, adverse excursion, and trade stagnation.
"""

from __future__ import annotations

import math
from typing import Optional

from app.pis.models import PISConfig, PositionObservation, ThesisDrift


class ThesisDriftEvaluator:
    """Evaluates position thesis health and degradation without numerical instability."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()

    def evaluate(
        self,
        obs: PositionObservation,
        trough_r: float,
        stagnation_bars: int,
    ) -> ThesisDrift:
        """Evaluate thesis drift for a position observation.

        Args:
            obs: PositionObservation snapshot.
            trough_r: Lowest R-multiple recorded for position.
            stagnation_bars: Count of consecutive bars with flat/no progress.

        Returns:
            ThesisDrift object.
        """
        # Adverse excursion magnitude (in R)
        adverse_r = abs(min(0.0, trough_r))
        if math.isnan(adverse_r) or math.isinf(adverse_r):
            adverse_r = 0.0

        # Score component based on adverse excursion (0.0 = terrible, 1.0 = no adverse excursion)
        adverse_penalty = max(0.0, min(1.0, adverse_r / 2.0))

        # Stagnation component (0.0 = terrible, 1.0 = fresh/moving)
        max_bars = max(1, self.config.stagnation_max_bars)
        stagnation_penalty = max(0.0, min(1.0, stagnation_bars / max_bars))

        # Composite drift score [0.0, 1.0] where higher = worse degradation
        drift_score = max(0.0, min(1.0, (adverse_penalty * 0.6) + (stagnation_penalty * 0.4)))

        is_degraded = drift_score >= 0.5 or stagnation_bars >= max_bars or adverse_r >= 1.5

        return ThesisDrift(
            drift_score=drift_score,
            adverse_excursion_r=adverse_r,
            stagnation_bars=stagnation_bars,
            is_degraded=is_degraded,
        )
