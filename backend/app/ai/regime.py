"""Market regime detection.

Classifies each symbol's current tape into trend-up / trend-down / range /
volatile using the Kaufman efficiency ratio, EMA slope and the ATR
percentile. The strategy pipeline reweights strategies by regime so the
system adapts as conditions change.
"""

from __future__ import annotations

import pandas as pd

from app.core.domain import Regime

ER_TREND_THRESHOLD = 0.30
VOL_PERCENTILE_THRESHOLD = 0.85
SLOPE_LOOKBACK = 10


def detect_regime(df: pd.DataFrame) -> tuple[Regime, dict]:
    """Classify the latest bar of an enriched frame. Returns (regime, details)."""
    details: dict = {"er": None, "slope_pct": None, "vol_percentile": None}
    if df.empty or len(df) < 60 or "er20" not in df.columns:
        return Regime.RANGE, details

    er = df["er20"].iloc[-1]
    atr_pct = (df["atr14"] / df["close"] * 100.0).tail(390)
    current_vol = atr_pct.iloc[-1]
    vol_percentile = float((atr_pct <= current_vol).mean()) if pd.notna(current_vol) else 0.5

    ema = df["ema20"]
    slope_pct = 0.0
    if len(ema) > SLOPE_LOOKBACK and pd.notna(ema.iloc[-1]) and pd.notna(ema.iloc[-1 - SLOPE_LOOKBACK]):
        slope_pct = float((ema.iloc[-1] / ema.iloc[-1 - SLOPE_LOOKBACK] - 1.0) * 100.0)

    er_val = float(er) if pd.notna(er) else 0.0
    details = {
        "er": round(er_val, 3),
        "slope_pct": round(slope_pct, 4),
        "vol_percentile": round(vol_percentile, 3),
    }

    if vol_percentile >= VOL_PERCENTILE_THRESHOLD:
        return Regime.VOLATILE, details
    if er_val >= ER_TREND_THRESHOLD:
        return (Regime.TREND_UP if slope_pct > 0 else Regime.TREND_DOWN), details
    return Regime.RANGE, details


REGIME_LABELS = {
    Regime.TREND_UP: "Trending Up",
    Regime.TREND_DOWN: "Trending Down",
    Regime.RANGE: "Range-bound",
    Regime.VOLATILE: "High Volatility",
}
