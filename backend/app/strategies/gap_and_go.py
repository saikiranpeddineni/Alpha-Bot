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
        "min_gap_pct": 2.0,
        "vol_z_min": 1.5,
        "start_time": "09:35",
        "end_time": "11:30",
        "reward_risk": 2.5,
        "stop_buffer": 0.05,
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

        p = self.params
        start_t = time.fromisoformat(p["start_time"])
        end_t = time.fromisoformat(p["end_time"])
        if not (start_t <= now_time <= end_t):
            return None

        # Prefer scanner mover context. If it is unavailable, derive the same gap
        # from the existing intraday frame so the strategy is not silently dead
        # when the scanner metadata path is temporarily absent.
        gap_pct = ctx.mover_change_pct
        if gap_pct is None or abs(float(gap_pct)) < 1e-12:
            today_mask = et_dates == today
            today_df = df.loc[today_mask]
            prior_df = df.loc[~today_mask]
            if not today_df.empty and not prior_df.empty:
                prior_close = float(prior_df["close"].iloc[-1])
                today_open = float(today_df["open"].iloc[0])
                if prior_close > 0:
                    gap_pct = (today_open / prior_close - 1.0) * 100.0

        if gap_pct is None:
            return None

        gap_pct = float(gap_pct)
        min_gap = float(p["min_gap_pct"])
        reward_risk = float(p["reward_risk"])
        stop_buffer = float(p["stop_buffer"])
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

        if vol_z < float(p["vol_z_min"]):
            return None

        ema_spread_pct = abs(ema9 - ema20) / ema20 * 100.0
        confidence = min(0.90, 0.55 + (0.05 * max(0.0, vol_z)) + (0.05 * min(2.0, ema_spread_pct)))

        if is_gap_up:
            if close_px > vwap and ema9 > ema20 and close_px > open_px:
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_LONG,
                    confidence=round(confidence, 4),
                    strategy=self.name,
                    reason=f"Gap up continuation breakout (vol z {vol_z:.1f})",
                    price=close_px,
                    score=0.75,
                    stop_loss=ema20 - stop_buffer,
                    take_profit=close_px + (close_px - ema20) * reward_risk,
                )

        elif is_gap_down:
            if close_px < vwap and ema9 < ema20 and close_px < open_px:
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_SHORT,
                    confidence=round(confidence, 4),
                    strategy=self.name,
                    reason=f"Gap down continuation breakdown (vol z {vol_z:.1f})",
                    price=close_px,
                    score=0.75,
                    stop_loss=ema20 + stop_buffer,
                    take_profit=close_px - (ema20 - close_px) * reward_risk,
                )

        return None
