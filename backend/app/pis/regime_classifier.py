"""PIS Market Regime Classifier Engine (Quadrant 1).

Classifies the active market microstructure and trend regime for the position:
- TREND_RUNNER: High momentum, strong directional volume expansion, high trend persistence.
- RANGE_BOUND: Oscillating around entry/mean, low directional volume, mean-reverting.
- VOLATILITY_EXPANSION: Wide bar spreads, high ATR expansion, rapid two-sided liquidity.
- CHOPPY_DEFENSIVE: Conflicting bars, lack of follow-through, heavy opposing absorption.
"""

from __future__ import annotations

from collections import deque

from app.pis.models import MarketRegime, PISConfig, PositionObservation, TradeHealth


class MarketRegimeClassifier:
    """Evaluates position history and market data to classify the active market regime."""

    def __init__(self, config: PISConfig | None = None) -> None:
        self.config = config or PISConfig()

    def classify_regime(
        self,
        obs: PositionObservation,
        history: deque[PositionObservation],
        health: TradeHealth,
    ) -> tuple[MarketRegime, float, str]:
        """Classify active market regime.

        Args:
            obs: Latest PositionObservation.
            history: Bounded observation history deque.
            health: Current TradeHealth breakdown.

        Returns:
            Tuple of (MarketRegime, confidence: float [0.0, 1.0], explanation: str).
        """
        if not getattr(self.config, "enable_market_regime_awareness", True) or len(history) < 2:
            return MarketRegime.TREND_RUNNER, 0.70, "Default Trend Runner Regime (Initialization)"

        side = obs.side
        atr = max(1e-6, obs.atr)
        n_bars = len(history)

        # 1. Measure Directional Progress & Velocity
        first_obs = history[0]
        net_price_delta = (obs.close - first_obs.close) * side
        net_r_velocity = (net_price_delta / atr) / max(1, n_bars)

        # 2. Measure Directional Volume Delta
        favorable_vol = 0.0
        adverse_vol = 0.0
        for h in history:
            bar_dir = (h.close - h.open) if hasattr(h, "open") else (h.close - h.low if side > 0 else h.high - h.close)
            if (side > 0 and h.close >= h.entry_price) or (side < 0 and h.close <= h.entry_price):
                favorable_vol += h.volume
            else:
                adverse_vol += h.volume

        total_vol = favorable_vol + adverse_vol
        vol_imbalance = (favorable_vol - adverse_vol) / max(1.0, total_vol)

        # 3. Measure Volatility Spread Ratio
        avg_range = sum(abs(h.high - h.low) for h in history) / max(1, n_bars)
        range_to_atr = avg_range / atr

        # 4. Classification Decision Logic
        # Case A: High Volatility Expansion (Wide spreads, large 2-sided wicks)
        if range_to_atr >= 2.20 and health.volatility_score <= 0.40:
            regime = MarketRegime.VOLATILITY_EXPANSION
            confidence = min(0.95, range_to_atr / 3.0)
            summary = f"High Volatility Expansion (Range/ATR: {range_to_atr:.2f}x, Rapid multi-directional liquidity)"

        # Case B: Choppy / Defensive (Adverse volume dominant, negative velocity, poor momentum)
        elif (vol_imbalance <= -0.40 and net_r_velocity <= -0.10) or (health.trend_score <= 0.30 and health.momentum_score <= 0.35):
            regime = MarketRegime.CHOPPY_DEFENSIVE
            confidence = max(0.60, min(0.90, abs(vol_imbalance)))
            summary = f"Choppy Defensive (Opposing Volume Delta: {vol_imbalance:+.2f}, Negative Velocity: {net_r_velocity:+.2f}R/bar)"

        # Case C: Strong Trend Runner (High health, positive directional volume, clean momentum)
        elif health.trend_score >= 0.65 and health.momentum_score >= 0.55 and vol_imbalance >= 0.15:
            regime = MarketRegime.TREND_RUNNER
            confidence = min(0.95, (health.trend_score * 0.5) + (health.momentum_score * 0.5))
            summary = f"Explosive Trend Expansion (Trend: {health.trend_score:.2f}, Volume Imbalance: {vol_imbalance:+.2f}, Smooth Progression)"

        # Case D: Range-Bound / Mean Reverting (Flat net velocity, moderate range, low directional delta)
        elif abs(net_r_velocity) < 0.08 and health.trend_score <= 0.60:
            regime = MarketRegime.RANGE_BOUND
            confidence = max(0.60, min(0.85, 1.0 - abs(net_r_velocity)))
            summary = f"Range-Bound Mean Reversion (Flat Velocity: {net_r_velocity:+.2f}R/bar, Oscillating Inside Range)"

        # Default fallback to Trend Runner
        else:
            regime = MarketRegime.TREND_RUNNER
            confidence = 0.65
            summary = f"Trend Developing (Trend Score: {health.trend_score:.2f}, Volume Delta: {vol_imbalance:+.2f})"

        return regime, round(confidence, 3), summary
