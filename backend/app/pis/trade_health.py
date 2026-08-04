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

        # 1. Trend Score: Regime-aware structural retention
        current_r = obs.r_multiple
        if math.isnan(current_r) or math.isinf(current_r):
            current_r = 0.0

        if peak_r >= 0.60:
            # Established Trend: evaluate retention relative to initial risk cushion
            # Prevent single-bar pullbacks from crashing trend score to zero
            retention = max(0.0, min(1.0, current_r / peak_r)) if peak_r > 0 else 0.5
            trend_score = max(0.20, retention) if current_r > 0 else max(0.0, 0.20 + (current_r * 0.50))
        else:
            # Base Building / Discovery: evaluate health against initial risk boundary (-1.0R to +0.6R)
            # Give trade room to develop inside its risk envelope
            trend_score = max(0.0, min(1.0, 0.60 + (current_r * 0.40)))

        # 2. Momentum Score: Noise-dampened intra-bar & directional evaluation
        bar_range = obs.high - obs.low
        if bar_range <= 1e-8 or math.isnan(bar_range) or math.isinf(bar_range):
            momentum_score = 0.55
        elif bar_range < 0.35 * atr:
            # Micro-consolidation bar (tight range noise): Treat as healthy consolidation
            momentum_score = 0.65
        else:
            if obs.side > 0:  # Long
                pos_in_bar = (obs.close - obs.low) / bar_range
            else:  # Short
                pos_in_bar = (obs.high - obs.close) / bar_range
            # Dampen extreme 0.0/1.0 intra-bar spikes to preserve multi-bar trend context
            momentum_score = max(0.15, min(0.95, (pos_in_bar * 0.70) + 0.15))

        # 3. Volatility Score: ratio of bar range to ATR (excessive range indicates exhaustion/distribution)
        vol_ratio = bar_range / atr
        if math.isnan(vol_ratio) or math.isinf(vol_ratio) or bar_range <= 1e-8:
            volatility_score = 0.70
        elif vol_ratio > 3.5:
            volatility_score = 0.35
        elif vol_ratio > 2.0:
            volatility_score = 0.60
        else:
            volatility_score = 0.85

        # 4. Volume Score: Price-Volume Distribution & Absorption Intelligence
        # Distinguish high-volume institutional selling (distribution) from low-volume absorption (consolidation)
        is_adverse_bar = (obs.side > 0 and obs.close < obs.high) or (obs.side < 0 and obs.close > obs.low)
        if obs.volume <= 0:
            volume_score = 0.60
        elif is_adverse_bar and vol_ratio > 2.5:
            # Wide spread adverse bar on extreme expansion: distribution pressure
            volume_score = 0.30
        elif is_adverse_bar:
            # Normal pullback / low-vol absorption: healthy consolidation
            volume_score = 0.80
        else:
            # Favorable expansion on volume
            volume_score = 0.90

        # Weighted overall composite health (inject news sentiment if extreme)
        sentiment_boost = obs.news_sentiment_score * 0.15
        overall = (
            trend_score * 0.45
            + momentum_score * 0.25
            + volume_score * 0.15
            + volatility_score * 0.15
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

