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
        peak_r: float = 0.0,
    ) -> ThesisDrift:
        """Evaluate thesis drift for a position observation.

        Args:
            obs: PositionObservation snapshot.
            trough_r: Lowest R-multiple recorded for position.
            stagnation_bars: Count of consecutive bars with flat/no progress.
            peak_r: Highest R-multiple recorded for position.

        Returns:
            ThesisDrift object.
        """
        # Adverse excursion magnitude (in R)
        adverse_r = abs(min(0.0, trough_r))
        if math.isnan(adverse_r) or math.isinf(adverse_r):
            adverse_r = 0.0

        # Score component based on adverse excursion (0.0 = clean, 1.0 = severe adverse pressure)
        adverse_penalty = max(0.0, min(1.0, adverse_r / 0.80))

        # Stagnation component (0.0 = fresh/moving, 1.0 = completely stalled)
        max_bars = max(1, self.config.stagnation_max_bars)
        stagnation_penalty = max(0.0, min(1.0, stagnation_bars / max_bars))

        # Composite drift score [0.0, 1.0] where higher = worse degradation
        drift_score = max(0.0, min(1.0, (adverse_penalty * 0.70) + (stagnation_penalty * 0.30)))

        # Structural invalidation:
        # 1. Early sustained adverse breakdown (dropped >= 0.40R for >= 2 bars before establishing >0.50R trend)
        # 2. General severe adverse excursion (>= 0.75R)
        # 3. Complete momentum stagnation (>= max_bars)
        early_adverse_breakdown = (adverse_r >= 0.40 and peak_r < 0.50 and stagnation_bars >= 2)
        is_degraded = drift_score >= 0.50 or stagnation_bars >= max_bars or adverse_r >= 0.75 or early_adverse_breakdown

        return ThesisDrift(
            drift_score=drift_score,
            adverse_excursion_r=adverse_r,
            stagnation_bars=stagnation_bars,
            is_degraded=is_degraded,
        )
