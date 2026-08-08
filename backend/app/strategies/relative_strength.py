"""Relative Strength Strategy.

Cross-Asset Momentum Alpha.
Triggers when a symbol has extreme relative strength compared to the SPY,
heavy accumulation volume, and makes local highs.
"""

from __future__ import annotations

import pandas as pd
from datetime import time
from zoneinfo import ZoneInfo

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class RelativeStrength(Strategy):
    name = "relative_strength"
    description = "Trades institutional leaders and laggards relative to SPY"
    
    is_breakout = False
    ml_threshold = 0.54
    evaluation_horizons = ["close"]
    regime_weights = {
        Regime.TREND_UP: 1.20, Regime.TREND_DOWN: 0.80, Regime.RANGE: 1.00, Regime.VOLATILE: 0.90
    }
    default_params = {
        "rs_long_threshold": 0.75,    # 0.75 to 1.0 means strong outperformance vs SPY
        "rs_short_threshold": 0.25,   # 0.0 to 0.25 means strong underperformance vs SPY
        "vol_z_min": 1.2,             # Require volume conviction
        "reward_risk": 2.5,
    }

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        super().__init__(params, enabled)
        self._traded: set[tuple[str, object]] = set()

    def reset(self) -> None:
        self._traded.clear()

    def on_entry_filled(self, symbol: str, timestamp: object) -> None:
        dt = getattr(timestamp, "date", lambda: timestamp)()
        self._traded.add((symbol, dt))

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        if ctx.position is not None or len(df) < 30:
            return None

        # Requires RS score from the engine
        if ctx.rs_score is None:
            return None

        et_dates = df.index.tz_convert(ET).date
        et_times = df.index.tz_convert(ET).time
        today = et_dates[-1]
        now_time = et_times[-1]

        if (symbol, today) in self._traded:
            return None

        p = self.params

        last_row = df.iloc[-1]
        if "vol_z" not in last_row or pd.isna(last_row["vol_z"]):
            return None

        vol_z = float(last_row["vol_z"])
        if vol_z < p["vol_z_min"]:
            return None

        close_px = float(last_row["close"])
        
        # Calculate local 30-minute high/low
        local_df = df.iloc[-30:-1]
        if local_df.empty:
            return None
            
        local_high = float(local_df["high"].max())
        local_low = float(local_df["low"].min())

        # Long Setup (High Relative Strength)
        if ctx.rs_score >= p["rs_long_threshold"]:
            if close_px > local_high:
                confidence = min(0.90, 0.60 + 0.1 * (ctx.rs_score - p["rs_long_threshold"]))
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_LONG,
                    confidence=round(confidence, 4),
                    strategy=self.name,
                    reason=f"RS score {ctx.rs_score:.2f} > {p['rs_long_threshold']} and broke local high",
                    score=0.75,
                )

        # Short Setup (High Relative Weakness)
        if ctx.rs_score <= p["rs_short_threshold"]:
            if close_px < local_low:
                confidence = min(0.90, 0.60 + 0.1 * (p["rs_short_threshold"] - ctx.rs_score))
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_SHORT,
                    confidence=round(confidence, 4),
                    strategy=self.name,
                    reason=f"RS score {ctx.rs_score:.2f} < {p['rs_short_threshold']} and broke local low",
                    score=0.75,
                )

        return None
