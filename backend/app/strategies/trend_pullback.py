"""Trend Pullback / EMA Cloud Strategy.

Buys pullbacks in established trends instead of chasing breakouts.
Offers higher reward-to-risk (often 2.5R-3.5R).
"""

from __future__ import annotations

import pandas as pd
from datetime import time
from zoneinfo import ZoneInfo

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class TrendPullback(Strategy):
    name = "trend_pullback"
    description = "Buys pullbacks into the EMA20/50 value zone during established trends"
    
    is_breakout = False
    ml_threshold = 0.53
    evaluation_horizons = ["60m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.20, Regime.TREND_DOWN: 1.20, Regime.RANGE: 0.80, Regime.VOLATILE: 0.80
    }
    default_params = {
        "min_er20": 0.35,
        "max_vol_z": 0.5,
        "min_wick_ratio": 0.40,
        "value_zone_atr_mult": 0.5,
        "trend_spread_min_pct": 0.20,
        "long_rsi_min": 40.0,
        "long_rsi_max": 55.0,
        "short_rsi_min": 45.0,
        "short_rsi_max": 60.0,
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
        for col in ["er20", "ema20", "ema50", "rsi14", "vol_z", "atr14"]:
            if col not in df.columns or pd.isna(df[col].iloc[-1]):
                return None

        min_er20 = float(p["min_er20"])
        max_vol_z = float(p["max_vol_z"])
        min_wick_ratio = float(p["min_wick_ratio"])
        value_zone_atr_mult = float(p["value_zone_atr_mult"])
        trend_spread_min_pct = float(p["trend_spread_min_pct"]) / 100.0
        long_rsi_min = float(p["long_rsi_min"])
        long_rsi_max = float(p["long_rsi_max"])
        short_rsi_min = float(p["short_rsi_min"])
        short_rsi_max = float(p["short_rsi_max"])
        reward_risk = float(p["reward_risk"])

        last_row = df.iloc[-1]
        
        er20 = float(last_row["er20"])
        ema20 = float(last_row["ema20"])
        ema50 = float(last_row["ema50"])
        rsi14 = float(last_row["rsi14"])
        vol_z = float(last_row["vol_z"])
        atr14 = float(last_row["atr14"])
        
        close_px = float(last_row["close"])
        open_px = float(last_row["open"])
        high_px = float(last_row["high"])
        low_px = float(last_row["low"])
        
        candle_range = max(high_px - low_px, 1e-6)

        if er20 < min_er20:
            return None

        is_uptrend = (ema20 > ema50) and ((ema20 - ema50) / ema50 >= trend_spread_min_pct)
        is_downtrend = (ema20 < ema50) and ((ema50 - ema20) / ema50 >= trend_spread_min_pct)

        if vol_z > max_vol_z:
            return None

        # Long Setup (Uptrend Pullback)
        if is_uptrend:
            stop_loss = ema50 - value_zone_atr_mult * atr14
            if low_px <= ema20 and low_px >= stop_loss:
                if long_rsi_min <= rsi14 <= long_rsi_max:
                    lower_wick = min(open_px, close_px) - low_px
                    wick_ratio = lower_wick / candle_range
                    
                    if close_px > open_px and close_px > ema20 and wick_ratio >= min_wick_ratio:
                        confidence = min(0.90, 0.55 + (0.15 * min(2.0, er20)) + (0.10 * min(1.0, wick_ratio)))
                        return Signal(
                            symbol=symbol,
                            action=SignalAction.ENTER_LONG,
                            confidence=round(confidence, 4),
                            strategy=self.name,
                            reason=f"Trend pullback (er20 {er20:.2f}, wick {wick_ratio:.2f})",
                            price=close_px,
                            score=0.80,
                            stop_loss=stop_loss,
                            take_profit=close_px + (close_px - stop_loss) * reward_risk,
                        )

        # Short Setup (Downtrend Pullback)
        elif is_downtrend:
            stop_loss = ema50 + value_zone_atr_mult * atr14
            if high_px >= ema20 and high_px <= stop_loss:
                if short_rsi_min <= rsi14 <= short_rsi_max:
                    upper_wick = high_px - max(open_px, close_px)
                    wick_ratio = upper_wick / candle_range
                    
                    if close_px < open_px and close_px < ema20 and wick_ratio >= min_wick_ratio:
                        confidence = min(0.90, 0.55 + (0.15 * min(2.0, er20)) + (0.10 * min(1.0, wick_ratio)))
                        return Signal(
                            symbol=symbol,
                            action=SignalAction.ENTER_SHORT,
                            confidence=round(confidence, 4),
                            strategy=self.name,
                            reason=f"Trend pullback short (er20 {er20:.2f}, wick {wick_ratio:.2f})",
                            price=close_px,
                            score=0.80,
                            stop_loss=stop_loss,
                            take_profit=close_px - (stop_loss - close_px) * reward_risk,
                        )

        return None
