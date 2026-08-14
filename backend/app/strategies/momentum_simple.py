"""Lean production momentum strategy for AlphaDesk 007.

Only setup evidence needed for a momentum continuation is evaluated here:
MACD impulse, EMA trend, VWAP location, RSI, volume and scanner direction.
No news calls, memory gates, adaptive thresholds or portfolio logic live here.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")
EVENT_WINDOW_BARS = 10


@register
class MomentumSimple(Strategy):
    name = "momentum"
    description = "MACD/EMA/VWAP momentum continuation"
    is_breakout = True
    ml_threshold = 0.50
    regime_weights = {
        Regime.TREND_UP: 1.20,
        Regime.TREND_DOWN: 1.20,
        Regime.RANGE: 0.80,
        Regime.VOLATILE: 0.90,
    }
    default_params = {
        "rsi_min": 48.0,
        "rsi_max": 80.0,
        "vol_z_min": -0.25,
        "event_window_bars": EVENT_WINDOW_BARS,
        "atr_stop_mult": 2.0,
        "reward_risk": 1.8,
        "min_minutes_since_open": 5,
    }

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        super().__init__(params, enabled)
        self._events: dict[str, dict[str, dict[str, object]]] = {}

    def reset(self) -> None:
        self._events.clear()

    def _events_for(self, symbol: str) -> dict[str, dict[str, object]]:
        return self._events.setdefault(symbol.upper(), {})

    def _arm(self, symbol: str, direction: str, timestamp) -> None:
        self._events_for(symbol)[direction] = {"origin": timestamp, "emitted": False}

    def signal_accepted(self, sig: Signal) -> None:
        direction = "long" if sig.action == SignalAction.ENTER_LONG else "short"
        event = self._events_for(sig.symbol).get(direction)
        if event is not None:
            event["emitted"] = True

    def _event_valid(self, symbol: str, direction: str, df: pd.DataFrame) -> bool:
        event = self._events_for(symbol).get(direction)
        if not event or bool(event.get("emitted")):
            return False
        origin = event.get("origin")
        if origin is None:
            return False
        try:
            origin_idx = df.index.get_loc(origin)
        except (KeyError, TypeError):
            return False
        if len(df) - 1 - int(origin_idx) > int(self.params.get("event_window_bars", EVENT_WINDOW_BARS)):
            self._events_for(symbol).pop(direction, None)
            return False
        return True

    @staticmethod
    def _quality(
        side: str,
        close: float,
        vwap: float,
        atr: float,
        vol_z: float,
        rs: float | None,
        mover_bias: str | None,
    ) -> float:
        score = 55.0
        if side == "long" and close > vwap:
            score += 12.0
        if side == "short" and close < vwap:
            score += 12.0
        if vol_z >= 0.5:
            score += 8.0
        elif vol_z < -0.5:
            score -= 8.0
        if rs is not None:
            if side == "long":
                score += 10.0 if rs >= 0.60 else (-8.0 if rs < 0.40 else 0.0)
            else:
                score += 10.0 if rs <= 0.40 else (-8.0 if rs > 0.60 else 0.0)
        if (side == "long" and mover_bias == "gainer") or (side == "short" and mover_bias == "loser"):
            score += 7.0
        if atr > 0:
            extension = abs(close - vwap) / atr
            if extension <= 1.5:
                score += 4.0
            elif extension > 2.5:
                score -= 8.0
        return max(0.0, min(100.0, score))

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        if len(df) < 30:
            return None
        required = ("ema9", "ema20", "vwap", "macd_hist", "rsi14", "atr14", "vol_z")
        if any(c not in df.columns for c in required):
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]
        if any(pd.isna(last[c]) for c in required) or any(pd.isna(prev[c]) for c in ("ema9", "ema20", "macd_hist")) or pd.isna(prev2["macd_hist"]):
            return None

        close = float(last["close"])
        atr = float(last["atr14"])
        rsi = float(last["rsi14"])
        vol_z = float(last["vol_z"])
        if close <= 0 or atr <= 0 or vol_z < float(self.params["vol_z_min"]):
            return None

        hist = float(last["macd_hist"])
        hist_prev = float(prev["macd_hist"])
        hist_prev2 = float(prev2["macd_hist"])
        now = df.index[-1]
        if hist > hist_prev and hist_prev <= hist_prev2:
            self._arm(symbol, "long", now)
        if hist < hist_prev and hist_prev >= hist_prev2:
            self._arm(symbol, "short", now)

        local = ctx.now.astimezone(ET)
        minutes = local.hour * 60 + local.minute
        if minutes < 9 * 60 + 30 + int(self.params.get("min_minutes_since_open", 5)):
            return None

        long_aligned = float(last["ema9"]) > float(last["ema20"]) and close > float(last["vwap"])
        short_aligned = float(last["ema9"]) < float(last["ema20"]) and close < float(last["vwap"])
        mover = ctx.mover_bias

        long_trigger = self._event_valid(symbol, "long", df) or (mover == "gainer" and long_aligned and vol_z >= 0.5)
        short_trigger = self._event_valid(symbol, "short", df) or (mover == "loser" and short_aligned and vol_z >= 0.5)

        if long_aligned and long_trigger and float(self.params["rsi_min"]) <= rsi <= float(self.params["rsi_max"]):
            quality = self._quality("long", close, float(last["vwap"]), atr, vol_z, ctx.rs_score, mover)
            stop = close - float(self.params["atr_stop_mult"]) * atr
            target = close + float(self.params["atr_stop_mult"]) * float(self.params["reward_risk"]) * atr
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=min(0.95, 0.55 + max(0.0, vol_z) * 0.04 + (0.08 if mover == "gainer" else 0.0)),
                strategy=self.name,
                reason=f"momentum continuation; quality={quality:.0f}",
                price=close,
                stop_loss=stop,
                take_profit=target,
                trend_score=quality,
                strategy_params={"entry_quality_score": quality},
            )

        if short_aligned and short_trigger and (100.0 - float(self.params["rsi_max"])) <= rsi <= (100.0 - float(self.params["rsi_min"])):
            quality = self._quality("short", close, float(last["vwap"]), atr, vol_z, ctx.rs_score, mover)
            stop = close + float(self.params["atr_stop_mult"]) * atr
            target = close - float(self.params["atr_stop_mult"]) * float(self.params["reward_risk"]) * atr
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=min(0.95, 0.55 + max(0.0, vol_z) * 0.04 + (0.08 if mover == "loser" else 0.0)),
                strategy=self.name,
                reason=f"momentum continuation; quality={quality:.0f}",
                price=close,
                stop_loss=stop,
                take_profit=target,
                trend_score=quality,
                strategy_params={"entry_quality_score": quality},
            )
        return None
