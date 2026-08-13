from __future__ import annotations

import math
from typing import Optional, Sequence

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
        history: Optional[Sequence[PositionObservation]] = None
    ) -> TradeHealth:
        """Evaluate trade health for a position observation."""
        atr = max(1e-6, obs.atr) if not math.isnan(obs.atr) and not math.isinf(obs.atr) else 1e-6
        risk = max(1e-6, obs.risk_per_share) if not math.isnan(obs.risk_per_share) and not math.isinf(obs.risk_per_share) else 1e-6

        current_r = obs.r_multiple
        if math.isnan(current_r) or math.isinf(current_r):
            current_r = 0.0

        bar_range = obs.high - obs.low
        if bar_range <= 1e-8 or math.isnan(bar_range) or math.isinf(bar_range):
            pos_in_bar = 0.5
        else:
            if obs.side > 0:  # Long
                pos_in_bar = (obs.close - obs.low) / bar_range
            else:  # Short
                pos_in_bar = (obs.high - obs.close) / bar_range

        # ---- CONTEXTUAL PERSISTENCE & STRUCTURAL DETERIORATION ----
        persistence_penalty = 1.0
        is_structurally_adverse = False
        adverse_score = 0.0
        
        if history and len(history) >= 2:
            recent = tuple(history)[-4:]
            
            for i in range(1, len(recent)):
                prev = recent[i-1]
                curr = recent[i]
                
                if obs.side > 0:
                    lower_close = curr.close < prev.close
                    lower_high = curr.high < prev.high
                    curr_bar_range = max(1e-6, curr.high - curr.low)
                    adverse_bar = (curr.close - curr.low) / curr_bar_range < 0.40
                else:
                    lower_close = curr.close > prev.close
                    lower_high = curr.low > prev.low
                    curr_bar_range = max(1e-6, curr.high - curr.low)
                    adverse_bar = (curr.high - curr.close) / curr_bar_range < 0.40

                high_volume = curr.volume > prev.volume
                
                if lower_close and adverse_bar:
                    bar_penalty = 1.0
                    if lower_high:
                        bar_penalty += 0.5
                    if high_volume:
                        bar_penalty += 0.5
                    adverse_score += bar_penalty
                else:
                    adverse_score = max(0.0, adverse_score - 1.0)
            
            if adverse_score > 0:
                is_structurally_adverse = (adverse_score >= 3.0)
                decay = math.exp(-0.25 * adverse_score) 
                persistence_penalty = max(0.40, decay)

        # 1. Trend Score: Regime-aware structural retention (ATR-normalized continuous confidence)
        if peak_r >= 0.60:
            price_drawdown = max(0.0, (peak_r - current_r) * risk)
            atr_drawdown = price_drawdown / atr
            
            atr_multiplier = math.exp(-0.25 * atr_drawdown)
            raw_retention = max(0.0, min(1.0, current_r / peak_r)) if peak_r > 0 else 0.5
            
            if is_structurally_adverse:
                retention = min(atr_multiplier, raw_retention * persistence_penalty)
            else:
                retention = max(raw_retention, atr_multiplier * persistence_penalty)
                
            trend_score = max(0.20, retention) if current_r > 0 else max(0.0, 0.20 + (current_r * 0.50))
        else:
            trend_score = max(0.0, min(1.0, 0.60 + (current_r * 0.40)))

        # 2. Momentum Score: Context-aware noise dampening
        if bar_range <= 1e-8 or math.isnan(bar_range) or math.isinf(bar_range):
            momentum_score = 0.55
        elif bar_range < 0.35 * atr:
            momentum_score = 0.65
        else:
            raw_momentum = max(0.15, min(0.95, (pos_in_bar * 0.70) + 0.15))
            if not is_structurally_adverse and raw_momentum < 0.35:
                momentum_score = 0.45
            else:
                momentum_score = raw_momentum * persistence_penalty

        # 3. Volatility Score: range to ATR
        vol_ratio = bar_range / atr
        if math.isnan(vol_ratio) or math.isinf(vol_ratio) or bar_range <= 1e-8:
            volatility_score = 0.70
        elif vol_ratio > 3.5:
            volatility_score = 0.35
        elif vol_ratio > 2.0:
            volatility_score = 0.60
        else:
            volatility_score = 0.85

        # 4. Volume Score: Price-Volume Distribution
        is_adverse_bar = pos_in_bar < 0.40
        if obs.volume <= 0:
            volume_score = 0.60
        elif is_adverse_bar and vol_ratio > 2.5:
            volume_score = 0.20 if is_structurally_adverse else 0.40
        elif is_adverse_bar:
            volume_score = 0.60 if is_structurally_adverse else 0.80
        else:
            volume_score = 0.90

        sentiment_boost = obs.news_sentiment_score * obs.side * 0.15
        overall = (
            trend_score * 0.45
            + momentum_score * 0.25
            + volume_score * 0.15
            + volatility_score * 0.15
        )
        structure_classification = "NORMAL"
        if is_structurally_adverse:
            structure_classification = "STRUCTURAL_BREAKDOWN"
        elif adverse_score > 0:
            structure_classification = "HEALTHY_PULLBACK"

        return TradeHealth(
            overall=overall,
            trend_score=trend_score,
            momentum_score=momentum_score,
            volatility_score=volatility_score,
            volume_score=volume_score,
            news_sentiment=obs.news_sentiment_score,
            structure_classification=structure_classification,
        )
