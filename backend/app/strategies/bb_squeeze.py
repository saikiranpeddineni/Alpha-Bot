"""Bollinger Band Squeeze Strategy.

Enters on volume-confirmed breakouts from low-volatility consolidation periods (squeezes).
"""

from __future__ import annotations

import pandas as pd
from datetime import datetime

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

EVENT_WINDOW_BARS = 3  # short confirmation window

@register
class BollingerBandSqueeze(Strategy):
    name = "bb_squeeze"
    description = "Volume-confirmed breakout from a Bollinger Band squeeze"
    
    is_breakout = True
    ml_threshold = 0.53
    evaluation_horizons = ["60m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.10, Regime.TREND_DOWN: 1.10, Regime.RANGE: 0.80, Regime.VOLATILE: 0.90
    }
    default_params = {
        "width_percentile_lookback": 100,
        "width_percentile_threshold": 0.15,
        "vol_z_min": 1.0,
        "atr_stop_mult": 2.0,
        "reward_risk": 2.0,
        "trailing_stop_atr_mult": 1.5,
        "use_trend_filter": True,
        "min_minutes_since_open": 5,
        "event_window_bars": EVENT_WINDOW_BARS,
    }

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        super().__init__(params, enabled)
        # {symbol: {"long": {"time": datetime, "emitted": bool, "bar_idx": int}, "short": ...}}
        self._events: dict[str, dict[str, dict]] = {}

    def reset(self) -> None:
        self._events.clear()

    def _set_event(self, symbol: str, side: str, event_time: datetime, bar_idx: int) -> None:
        if symbol not in self._events:
            self._events[symbol] = {}
        self._events[symbol][side] = {"time": event_time, "emitted": False, "bar_idx": bar_idx}

    def _check_and_consume_event(self, symbol: str, side: str, current_bar_idx: int, window: int) -> bool:
        if symbol not in self._events or side not in self._events[symbol]:
            return False
        
        event = self._events[symbol][side]
        if event["emitted"]:
            return False
            
        bars_elapsed = current_bar_idx - event["bar_idx"]
        # Must be within window, and > 0 (don't fire on the exact bar it was armed, wait for confirmation)
        if 0 < bars_elapsed <= window:
            event["emitted"] = True
            return True
            
        # Expired
        if bars_elapsed > window:
            self._events[symbol].pop(side, None)
            
        return False

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        p = self.params
        if ctx.position is not None:
            return None

        # Block the violently erratic opening auction
        from zoneinfo import ZoneInfo
        now_et = ctx.now.astimezone(ZoneInfo("America/New_York"))
        mins_since_open = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
        if mins_since_open < p.get("min_minutes_since_open", 5):
            return None

        lb = int(p["width_percentile_lookback"])
        if len(df) < lb + 5:
            return None

        last, prev = df.iloc[-1], df.iloc[-2]
        needed = ("bb_width", "bb_upper", "bb_lower", "vol_z", "atr14")
        if any(pd.isna(last.get(c)) for c in needed) or any(pd.isna(prev.get(c)) for c in ("bb_upper", "bb_lower")):
            return None

        close = float(last["close"])
        vol_z = float(last["vol_z"])
        atr = float(last["atr14"])
        if atr <= 0:
            return None
            
        current_bar_idx = len(df)
        window = int(p.get("event_window_bars", EVENT_WINDOW_BARS))

        # Check if squeezed within the last 5 bars (consolidation phase)
        width_series = df["bb_width"]
        squeeze_active = False
        for i in range(-5, 0):
            window_df = width_series.iloc[i - lb : i].dropna()
            if len(window_df) < lb // 2:
                continue
            val = width_series.iloc[i]
            pct = (window_df <= val).mean()
            if pct <= p["width_percentile_threshold"]:
                squeeze_active = True
                break

        if squeeze_active:
            crossed_up = prev["close"] <= prev["bb_upper"] and close > last["bb_upper"]
            crossed_down = prev["close"] >= prev["bb_lower"] and close < last["bb_lower"]

            if crossed_up and vol_z >= p["vol_z_min"]:
                self._set_event(symbol, "long", ctx.now, current_bar_idx)
            if crossed_down and vol_z >= p["vol_z_min"]:
                self._set_event(symbol, "short", ctx.now, current_bar_idx)

        # Check for confirmed event
        confirmed_long = self._check_and_consume_event(symbol, "long", current_bar_idx, window)
        confirmed_short = self._check_and_consume_event(symbol, "short", current_bar_idx, window)

        if not confirmed_long and not confirmed_short:
            return None
            
        # Ensure the breakout is still valid and hasn't totally collapsed back into the band
        if confirmed_long and close < last["bb_mid"]:
            return None
        if confirmed_short and close > last["bb_mid"]:
            return None

        # Check EMA trend alignment and AI market regime to filter out false breakouts (head-fakes)
        if p.get("use_trend_filter", True):
            ema20 = float(last.get("ema20", 0.0))
            ema50 = float(last.get("ema50", 0.0))
            if ema20 > 0 and ema50 > 0:
                if confirmed_long and ema20 < ema50:
                    return None
                if confirmed_short and ema20 > ema50:
                    return None

            from app.core.domain import Regime
            if ctx.regime == Regime.VOLATILE:
                return None

        confidence = min(0.85, 0.55 + 0.08 * max(0.0, vol_z))

        if confirmed_long:
            stop = close - p["atr_stop_mult"] * atr
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=confidence,
                strategy=self.name,
                reason=f"BB Squeeze breakout up confirmed (vol z {vol_z:.1f})",
                price=close,
                stop_loss=stop,
                take_profit=close + p["reward_risk"] * (close - stop),
            )
            
        if confirmed_short:
            stop = close + p["atr_stop_mult"] * atr
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=confidence,
                strategy=self.name,
                reason=f"BB Squeeze breakout down confirmed (vol z {vol_z:.1f})",
                price=close,
                stop_loss=stop,
                take_profit=close - p["reward_risk"] * (stop - close),
            )
            
        return None
