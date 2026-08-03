"""PIS Trade Health Evaluation Engine.

Evaluates multi-factor trade health [0.0, 1.0] across trend, momentum,
volatility, and volume sub-metrics with strict numerical stability.
"""

from __future__ import annotations

import math
from typing import Optional

from app.pis.models import PISConfig, PositionObservation, TradeHealth


class TradeHealthEvaluator:
    """Computes health scores clamped to [0.0, 1.0] without numerical hazards."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()

    def evaluate(
        self,
        obs: PositionObservation,
        peak_r: float,
        trough_r: float,
    ) -> TradeHealth:
        """Evaluate trade health for a position observation.

        Args:
            obs: PositionObservation.
            peak_r: Peak R achieved so far.
            trough_r: Trough R achieved so far.

        Returns:
            TradeHealth object containing breakdown and overall score [0.0, 1.0].
        """
        # Defensive numerical sanitization
        atr = max(1e-6, obs.atr) if not math.isnan(obs.atr) and not math.isinf(obs.atr) else 1e-6
        risk = max(1e-6, obs.risk_per_share) if not math.isnan(obs.risk_per_share) and not math.isinf(obs.risk_per_share) else 1e-6

        # 1. Trend Score: measures current R relative to peak R
        current_r = obs.r_multiple
        if math.isnan(current_r) or math.isinf(current_r):
            current_r = 0.0

        if peak_r > 0.1:
            retention = current_r / peak_r
            trend_score = max(0.0, min(1.0, retention))
        else:
            # Early trade: check if current_r is non-negative
            trend_score = 0.5 + max(-0.5, min(0.5, current_r * 0.5))

        # 2. Momentum Score: close position relative to high/low bar range
        bar_range = obs.high - obs.low
        if bar_range <= 1e-8 or math.isnan(bar_range) or math.isinf(bar_range):
            momentum_score = 0.5
        else:
            if obs.side > 0:  # Long
                pos_in_bar = (obs.close - obs.low) / bar_range
            else:  # Short
                pos_in_bar = (obs.high - obs.close) / bar_range
            momentum_score = max(0.0, min(1.0, pos_in_bar))

        # 3. Volatility Score: ratio of bar range to ATR (excessive range indicates distribution)
        vol_ratio = bar_range / atr
        if math.isnan(vol_ratio) or math.isinf(vol_ratio) or bar_range <= 1e-8:
            volatility_score = 0.5
        elif vol_ratio > 3.0:
            volatility_score = 0.2
        elif vol_ratio > 2.0:
            volatility_score = 0.5
        else:
            volatility_score = 0.9

        # 4. Volume Score: volume presence
        volume_score = 0.7 if obs.volume > 0 else 0.5

        # Weighted overall composite health (inject news sentiment if extreme)
        # If sentiment is very high, it boosts overall health. If very low, it drags it down.
        sentiment_boost = obs.news_sentiment_score * 0.2
        overall = (
            trend_score * 0.45
            + momentum_score * 0.30
            + volatility_score * 0.15
            + volume_score * 0.10
        )
        overall = max(0.0, min(1.0, overall + sentiment_boost))

        return TradeHealth(
            overall=overall,
            trend_score=trend_score,
            momentum_score=momentum_score,
            volatility_score=volatility_score,
            volume_score=volume_score,
            news_sentiment=obs.news_sentiment_score,
        )
