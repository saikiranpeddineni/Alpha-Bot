"""PIS Thesis Drift Tracking Engine.

Measures thesis degradation, adverse excursion, and trade stagnation.
"""

from __future__ import annotations

import math

from app.pis.models import PISConfig, PositionObservation, ThesisDrift


class ThesisDriftEvaluator:
    """Evaluates position thesis health and degradation without numerical instability."""

    def __init__(self, config: PISConfig | None = None) -> None:
        self.config = config or PISConfig()

    def evaluate(
        self,
        obs: PositionObservation,
        trough_r: float,
        stagnation_bars: int,
        peak_r: float = 0.0,
        history_len: int = 1,
    ) -> ThesisDrift:
        """Evaluate thesis drift for a position observation.

        Args:
            obs: PositionObservation snapshot.
            trough_r: Lowest R-multiple recorded for position.
            stagnation_bars: Count of consecutive bars with flat/no progress.
            peak_r: Highest R-multiple recorded for position.
            history_len: Total count of bars observed so far.

        Returns:
            ThesisDrift object.
        """
        # Adverse excursion magnitude (in R)
        adverse_r = abs(min(0.0, trough_r))
        if math.isnan(adverse_r) or math.isinf(adverse_r):
            adverse_r = 0.0

        # Score component based on adverse excursion (0.0 = clean, 1.0 = approaching stop boundary)
        adverse_penalty = max(0.0, min(1.0, adverse_r / 1.00))

        # Stagnation component (0.0 = fresh/moving, 1.0 = completely stalled)
        max_bars = max(1, self.config.stagnation_max_bars)
        stagnation_penalty = max(0.0, min(1.0, stagnation_bars / max_bars))

        # Composite drift score [0.0, 1.0] where higher = worse degradation
        drift_score = max(0.0, min(1.0, (adverse_penalty * 0.70) + (stagnation_penalty * 0.30)))

        # Early Thesis Invalidation (Quadrant 2 - Asymmetric Loss Reduction):
        # Cuts trades that show sustained structural failure and heavy adverse excursion (<= -0.55R)
        # with zero upside traction within the initial entry window, preserving capital before full -1.0R stop.
        early_invalidation = False
        if getattr(self.config, "enable_early_invalidation", True):
            max_early_bars = getattr(self.config, "early_invalidation_max_bars", 5)
            r_threshold = getattr(self.config, "early_invalidation_r_threshold", -0.55)
            if (
                history_len <= max_early_bars
                and obs.r_multiple <= r_threshold
                and peak_r <= 0.05
            ):
                # Check for volume/wick confirmation of aggressive rejection
                bar_range = obs.high - obs.low
                adverse_wick = (obs.high - obs.close) if obs.side > 0 else (obs.close - obs.low)
                wick_ratio = adverse_wick / max(1e-6, bar_range)
                if wick_ratio >= 0.60 or obs.r_multiple <= -0.65:
                    early_invalidation = True

        # Structural invalidation:
        # 1. Early thesis failure within entry window (Quadrant 2)
        # 2. Time-Decay Loss Harvesting: prolonged bleed without recovery (>= 15 bars at <= -0.4R)
        # 3. Severe adverse excursion approaching hard risk boundary (>= 0.90R)
        # 4. Complete momentum stagnation (>= max_bars)
        # 5. Composite drift score exceeding critical structural threshold (>= 0.70)
        time_decay_bleed = (history_len >= 15 and obs.r_multiple <= -0.40)

        is_degraded = (
            early_invalidation
            or time_decay_bleed
            or drift_score >= 0.70
            or stagnation_bars >= max_bars
            or adverse_r >= 0.90
        )

        return ThesisDrift(
            drift_score=drift_score,
            adverse_excursion_r=adverse_r,
            stagnation_bars=stagnation_bars,
            is_degraded=is_degraded,
            early_invalidation_detected=early_invalidation,
        )
