"""Gap & Go Strategy.

Exploits pre-market gap stocks with strong volume.
Leverages the scanner's mover context.
Uses VWAP + EMA confirmation and requires a bullish/bearish candle close.
"""

from __future__ import annotations

import pandas as pd
from datetime import time
from zoneinfo import ZoneInfo

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class GapAndGo(Strategy):
    name = "gap_and_go"
    description = "Pre-market catalyst continuation (Gap & Go)"
    
    is_breakout = True
    ml_threshold = 0.54
    evaluation_horizons = ["60m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.15, Regime.TREND_DOWN: 1.15, Regime.RANGE: 0.70, Regime.VOLATILE: 0.90
    }
    default_params = {
        "min_gap_pct": 2.0,           # Minimum gap percentage
        "vol_z_min": 1.5,             # Minimum volume Z-score
        "start_time": "09:35",        # Only trade after the first 5 minutes established
        "end_time": "11:30",          # Stop trading after morning drive
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
        if ctx.position is not None or df.empty:
            return None

        et_dates = df.index.tz_convert(ET).date
        et_times = df.index.tz_convert(ET).time
        today = et_dates[-1]
        now_time = et_times[-1]
        
        # Only trade once per symbol per day
        if (symbol, today) in self._traded:
            return None

        # Time gating (Morning Drive only)
        p = self.params
        start_t = time.fromisoformat(p["start_time"])
        end_t = time.fromisoformat(p["end_time"])
        if not (start_t <= now_time <= end_t):
            return None

        # Requires scanner gap context
        if ctx.mover_change_pct is None:
            return None

        gap_pct = ctx.mover_change_pct
        min_gap = p["min_gap_pct"]
        is_gap_up = gap_pct >= min_gap
        is_gap_down = gap_pct <= -min_gap

        if not (is_gap_up or is_gap_down):
            return None

        last_row = df.iloc[-1]
        close_px = float(last_row["close"])
        open_px = float(last_row["open"])
        vwap = float(last_row["vwap"])
        ema9 = float(last_row["ema9"])
        ema20 = float(last_row["ema20"])
        vol_z = float(last_row["vol_z"])

        # Require high relative volume
        if vol_z < p["vol_z_min"]:
            return None

        # Long Setup (Gap Up)
        if is_gap_up:
            if close_px > vwap and ema9 > ema20 and close_px > open_px:
                # Confirmed gap & go long
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_LONG,
                    strategy=self.name,
                    score=0.70, # Base score, ML pipeline will adjust
                )

        # Short Setup (Gap Down)
        elif is_gap_down:
            if close_px < vwap and ema9 < ema20 and close_px < open_px:
                # Confirmed gap & go short
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_SHORT,
                    strategy=self.name,
                    score=0.70,
                )

        return None
