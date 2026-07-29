from __future__ import annotations

import math
from typing import Any

import pandas as pd

from .models import PositionObservation


def _clip100(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _sigmoid_score(x: float, scale: float = 1.0) -> float:
    z = max(-8.0, min(8.0, x / max(scale, 1e-9)))
    return _clip100(100.0 / (1.0 + math.exp(-z)))


def observation_from_market_data(
    *,
    now,
    position: Any,
    df: pd.DataFrame,
    relative_strength: float | None = None,
    market_context: float | None = None,
) -> PositionObservation:
    """Build point-in-time PIS evidence from completed bars only.

    The newest row may still be forming in live trading, so PIS intentionally
    scores the prior completed bar. This biases the system against premature
    reactions to one-minute wicks and transient quote noise.
    """
    if df is None or len(df) < 3:
        raise ValueError("PIS requires at least three bars")
    completed = df.iloc[:-1]
    bar = completed.iloc[-1]
    close = float(bar["close"])
    side = 1.0 if float(position.qty) > 0 else -1.0

    atr = None
    if "atr14" in completed.columns and pd.notna(bar.get("atr14")):
        atr = max(0.0, float(bar["atr14"]))
    atr_norm = atr if atr and atr > 1e-9 else max(close * 0.005, 0.01)

    ema_fast = float(bar["ema9"]) if "ema9" in completed.columns and pd.notna(bar.get("ema9")) else float(completed["close"].tail(9).mean())
    ema_slow = float(bar["ema21"]) if "ema21" in completed.columns and pd.notna(bar.get("ema21")) else float(completed["close"].tail(21).mean())
    trend_edge = side * (ema_fast - ema_slow) / atr_norm
    price_edge = side * (close - ema_slow) / atr_norm
    trend = _clip100(0.65 * _sigmoid_score(trend_edge, 0.65) + 0.35 * _sigmoid_score(price_edge, 1.0))

    ret3 = float(completed["close"].pct_change(3).iloc[-1]) if len(completed) >= 4 else 0.0
    ret8 = float(completed["close"].pct_change(8).iloc[-1]) if len(completed) >= 9 else ret3
    atr_pct = atr_norm / max(close, 1e-9)
    momentum_edge = side * (0.65 * ret3 + 0.35 * ret8) / max(atr_pct, 1e-5)
    momentum = _sigmoid_score(momentum_edge, 1.25)

    volume = None
    if "volume" in completed.columns:
        vol = float(bar["volume"])
        baseline = float(completed["volume"].iloc[:-1].tail(20).median()) if len(completed) > 2 else 0.0
        if baseline > 0:
            directional_bar = side * (float(bar["close"]) - float(bar["open"])) / atr_norm
            participation = min(3.0, vol / baseline)
            volume = _clip100(50.0 + 22.0 * directional_bar + 12.0 * (participation - 1.0))

    rs = None
    if relative_strength is not None:
        # Existing engine RS is centered near zero. Convert to a bounded score
        # while preserving direction for long/short positions.
        rs = _sigmoid_score(side * float(relative_strength), 0.75)

    vwap_distance_atr = None
    if "vwap" in completed.columns and pd.notna(bar.get("vwap")):
        vwap_distance_atr = side * (close - float(bar["vwap"])) / atr_norm

    return PositionObservation(
        timestamp=now,
        price=close,
        trend=trend,
        momentum=momentum,
        volume=volume,
        relative_strength=rs,
        market_context=market_context,
        atr=atr,
        vwap_distance_atr=vwap_distance_atr,
        metadata={"evidence_bar": str(completed.index[-1]), "causal": True},
    )
