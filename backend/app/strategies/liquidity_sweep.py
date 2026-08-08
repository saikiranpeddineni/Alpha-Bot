"""Liquidity Sweep Reversal Strategy.

Smart-money false breakout strategy.
Best for range or volatile markets.
Tight stops with potentially high R multiples.
"""

from __future__ import annotations

import pandas as pd
from datetime import time
from zoneinfo import ZoneInfo

from app.core.domain import Signal, SignalAction, Regime
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class LiquiditySweep(Strategy):
    name = "liquidity_sweep"
    description = "Trades false breakouts beyond session highs/lows with tight stops"
    
    is_breakout = False
    ml_threshold = 0.52
    evaluation_horizons = ["30m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.00, Regime.TREND_DOWN: 1.00, Regime.RANGE: 1.20, Regime.VOLATILE: 1.10
    }
    default_params = {
        "sweep_tolerance_pct": 0.30,  # Max allowable extension past level to still be a 'sweep'
        "whipsaw_ratio_min": 0.50,    # Wick must be at least 50% of the candle
        "vol_z_min": 1.5,             # Volume must spike on the sweep
        "start_time": "10:15",        # Wait for the morning range to establish
        "end_time": "15:30",          
        "reward_risk": 3.0,
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

        # Only trade in chop/range environments
        if ctx.regime not in (Regime.RANGE, Regime.VOLATILE):
            return None

        et_dates = df.index.tz_convert(ET).date
        et_times = df.index.tz_convert(ET).time
        today = et_dates[-1]
        now_time = et_times[-1]

        if (symbol, today) in self._traded:
            return None

        p = self.params
        start_t = time.fromisoformat(p["start_time"])
        end_t = time.fromisoformat(p["end_time"])
        if not (start_t <= now_time <= end_t):
            return None

        last_row = df.iloc[-1]
        for col in ["vol_z", "whipsaw_ratio"]:
            if col not in last_row or pd.isna(last_row[col]):
                return None

        vol_z = float(last_row["vol_z"])
        whipsaw_ratio = float(last_row["whipsaw_ratio"])
        
        if vol_z < p["vol_z_min"] or whipsaw_ratio < p["whipsaw_ratio_min"]:
            return None

        close_px = float(last_row["close"])
        open_px = float(last_row["open"])
        high_px = float(last_row["high"])
        low_px = float(last_row["low"])

        # Calculate session high/low up to the previous bar
        # This represents the established level that gets swept
        session_df = df.iloc[:-1]
        if session_df.empty:
            return None
            
        session_high = float(session_df["high"].max())
        session_low = float(session_df["low"].min())

        # Sweep High (Short Setup)
        # Price went above session high, but closed back below it
        if high_px > session_high and close_px < session_high:
            sweep_extension = ((high_px - session_high) / session_high) * 100.0
            if sweep_extension <= p["sweep_tolerance_pct"]:
                if close_px < open_px: # Bearish rejection
                    sweep_score = max(0, (p["sweep_tolerance_pct"] - sweep_extension) / p["sweep_tolerance_pct"])
                    confidence = min(0.90, 0.55 + (0.1 * vol_z) + (0.1 * sweep_score))
                    return Signal(
                        symbol=symbol,
                        action=SignalAction.ENTER_SHORT,
                        confidence=round(confidence, 4),
                        strategy=self.name,
                        reason=f"Sweep high breakdown (ext {sweep_extension:.2f}%, vol {vol_z:.1f})",
                        score=0.75,
                    )

        # Sweep Low (Long Setup)
        # Price went below session low, but closed back above it
        if low_px < session_low and close_px > session_low:
            sweep_extension = ((session_low - low_px) / session_low) * 100.0
            if sweep_extension <= p["sweep_tolerance_pct"]:
                if close_px > open_px: # Bullish rejection
                    sweep_score = max(0, (p["sweep_tolerance_pct"] - sweep_extension) / p["sweep_tolerance_pct"])
                    confidence = min(0.90, 0.55 + (0.1 * vol_z) + (0.1 * sweep_score))
                    return Signal(
                        symbol=symbol,
                        action=SignalAction.ENTER_LONG,
                        confidence=round(confidence, 4),
                        strategy=self.name,
                        reason=f"Sweep low breakout (ext {sweep_extension:.2f}%, vol {vol_z:.1f})",
                        score=0.75,
                    )

        return None
