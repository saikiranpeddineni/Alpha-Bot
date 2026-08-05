"""EMA momentum crossover with MACD-inflection pending event design.

Entry flow:
  1. MACD histogram inflection (histogram turns up/down on the current bar)
     creates a pending momentum event for the symbol/direction.
  2. The event stays valid for up to EVENT_WINDOW_BARS completed 1-minute bars.
  3. On each subsequent bar, if the standard confirmation conditions align
     (EMA trend direction, price vs VWAP, RSI range, volume z-score), a
     candidate signal is emitted and the event is consumed.
  4. Only one candidate is emitted per event.  After expiry or consumption a
     new inflection is required to re-arm.

Exit: EMA9/EMA20 cross-reversal while a momentum position is open.
"""

from __future__ import annotations

import logging

import pandas as pd

from app.core.domain import Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

logger = logging.getLogger("alphadesk")

EVENT_WINDOW_BARS = 10  # max bars a pending MACD event stays valid


@register
class MomentumCross(Strategy):
    name = "momentum"
    description = "EMA9/EMA20 trend with MACD-inflection pending event confirmation"

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        super().__init__(params, enabled)
        # {symbol: {"long": {time, emitted}, "short": {time, emitted}}}
        self._events: dict[str, dict[str, dict]] = {}

    def reset(self) -> None:
        self._events.clear()

    default_params = {
        "rsi_min": 50.0,
        "rsi_max": 78.0,
        "vol_z_min": 0.0,
        "event_window_bars": 10,
        "atr_stop_mult": 2.0,
        "reward_risk": 1.8,
        "trailing_stop_atr_mult": 1.5,
        "htf_trend_filter": False,
        "midday_block": False,
        "min_minutes_since_open": 5,
    }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _debug(self, ctx: StrategyContext, msg: str) -> None:
        if ctx.debug_mode:
            logger.info("momentum", msg)

    def _sym_events(self, symbol: str) -> dict[str, dict]:
        return self._events.setdefault(symbol, {})

    def _set_event(self, ctx: StrategyContext, symbol: str, direction: str, bar_time) -> None:
        evt = self._sym_events(symbol)
        existing = evt.get(direction)
        if existing is None or existing.get("emitted", False):
            evt[direction] = {"origin_time": bar_time, "latest_time": bar_time, "emitted": False}
            self._debug(
                ctx,
                f"[momentum] MACD_EVENT_CREATED sym={symbol} dir={direction} event_ts={bar_time}",
            )
        else:
            # Keep original inflection origin_time to prevent event age reset
            existing["latest_time"] = bar_time
            self._debug(
                ctx,
                f"[momentum] MACD_EVENT_UPDATED sym={symbol} dir={direction} origin_ts={existing.get('origin_time')} latest_ts={bar_time}",
            )

    def _consume_event(self, ctx: StrategyContext, symbol: str, direction: str, candidate_time) -> None:
        evt = self._sym_events(symbol).get(direction, {})
        event_time = evt.get("origin_time")
        self._sym_events(symbol)[direction]["emitted"] = True
        self._debug(
            ctx,
            f"[momentum] MACD_EVENT_CONSUMED sym={symbol} dir={direction} "
            f"event_ts={event_time} candidate_ts={candidate_time}",
        )

    def _expire_event(self, ctx: StrategyContext, symbol: str, direction: str, current_time) -> None:
        evt = self._sym_events(symbol).get(direction, {})
        event_time = evt.get("origin_time")
        del self._sym_events(symbol)[direction]
        self._debug(
            ctx,
            f"[momentum] MACD_EVENT_EXPIRED sym={symbol} dir={direction} "
            f"event_ts={event_time} current_ts={current_time}",
        )

    def _valid_event(
        self,
        ctx: StrategyContext,
        symbol: str,
        direction: str,
        current_bar_idx: int,
        df: pd.DataFrame,
    ) -> bool:
        """Return True if a pending (unconsumed, unexpired) event exists."""
        evt = self._sym_events(symbol).get(direction)
        if evt is None or evt.get("emitted"):
            return False
        event_time = evt.get("origin_time")
        if event_time is None:
            return False
        # Find the bar index of the event
        try:
            event_idx = df.index.get_loc(event_time)
        except KeyError:
            # Event time not in current window; treat as expired
            self._expire_event(ctx, symbol, direction, df.index[current_bar_idx])
            return False
        bars_elapsed = current_bar_idx - event_idx
        max_bars = int(self.params.get("event_window_bars", EVENT_WINDOW_BARS))
        if bars_elapsed > max_bars:
            self._expire_event(ctx, symbol, direction, df.index[current_bar_idx])
            return False
        age = bars_elapsed
        self._debug(
            ctx,
            f"[momentum] MACD_EVENT_CONFIRMED sym={symbol} dir={direction} "
            f"event_ts={event_time} age_bars={age} candidate_ts={df.index[current_bar_idx]}",
        )
        return True

    # ------------------------------------------------------------------
    # Main bar handler
    # ------------------------------------------------------------------

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        p = self.params
        if len(df) < 30:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]

        needed = ("ema9", "ema20", "vwap", "macd_hist", "rsi14", "atr14", "vol_z")
        if any(pd.isna(last.get(c)) for c in needed) or any(
            pd.isna(prev.get(c)) for c in ("ema9", "ema20", "macd_hist")
        ) or pd.isna(prev2.get("macd_hist")):
            return None

        close = float(last["close"])
        atr = float(last["atr14"])
        rsi = float(last["rsi14"])
        vol_z = float(last["vol_z"])
        current_bar_idx = len(df) - 1
        current_time = df.index[-1]

        # ---- EMA cross series (used for exit management and trend direction) ----
        crossed_up_series = (df["ema9"].shift(1) <= df["ema20"].shift(1)) & (df["ema9"] > df["ema20"])
        crossed_down_series = (df["ema9"].shift(1) >= df["ema20"].shift(1)) & (df["ema9"] < df["ema20"])
        crossed_up = bool(crossed_up_series.iloc[-1])
        crossed_down = bool(crossed_down_series.iloc[-1])

        # ---- Exit management for an open momentum position ----
        if ctx.position is not None:
            # Let the PIS manage exits (trailing stops and momentum decay)
            return None

        # ---- Time and Slippage Guards ----
        from zoneinfo import ZoneInfo
        now_et = ctx.now.astimezone(ZoneInfo("America/New_York"))
        mins_et = now_et.hour * 60 + now_et.minute

        # Optional midday block
        if p.get("midday_block", False) and (11 * 60 + 30 <= mins_et <= 13 * 60 + 30):
            return None

        # ---- HTF trend filter ----
        if p.get("htf_trend_filter", False) and (crossed_up or crossed_down):
            df_15 = df.resample("15Min").agg({"close": "last"}).dropna()
            if len(df_15) >= 20:
                ema20_15 = df_15["close"].ewm(span=20, adjust=False).mean()
                ema_15_rising = ema20_15.iloc[-1] > ema20_15.iloc[-2]
                if crossed_up and not ema_15_rising:
                    return None
                if crossed_down and ema_15_rising:
                    return None

        # ---- Volume / ATR guard ----
        if atr <= 0 or vol_z < p["vol_z_min"]:
            return None

        confidence = min(
            0.90,
            0.55
            + 0.05 * max(0.0, vol_z)
            + 0.1 * (float(last["er20"]) if pd.notna(last.get("er20")) else 0.0)
            + (0.10 if ctx.mover_bias is not None else 0.0)  # +10% for confirmed movers
        )

        # ---- Mover directional bias ----
        # top_gainer → long bias only; top_loser → short bias only; None → both
        bias = ctx.mover_bias   # "gainer" | "loser" | None
        allow_long  = bias != "loser"   # neutrals + gainers can go long
        allow_short = bias != "gainer"  # neutrals + losers can go short
        if bias is not None:
            self._debug(
                ctx,
                f"[momentum] MOVER_BIAS sym={symbol} bias={bias} "
                f"chg={ctx.mover_change_pct:+.1f}% allow_long={allow_long} allow_short={allow_short}",
            )

        # ---- MACD inflection detection ----
        hist_now = float(last["macd_hist"])
        hist_prev = float(prev["macd_hist"])
        hist_prev2 = float(prev2["macd_hist"])

        macd_inflected_up = hist_now > hist_prev and hist_prev <= hist_prev2
        macd_inflected_down = hist_now < hist_prev and hist_prev >= hist_prev2

        # Create new events (a fresh inflection resets the prior event for that direction)
        if macd_inflected_up:
            self._set_event(ctx, symbol, "long", current_time)
        if macd_inflected_down:
            self._set_event(ctx, symbol, "short", current_time)

        # ---- Long candidate ----
        if (
            allow_long
            and last["ema9"] > last["ema20"]
            and close > float(last["vwap"])
            and p["rsi_min"] <= rsi <= p["rsi_max"]
            and self._valid_event(ctx, symbol, "long", current_bar_idx, df)
        ):
            # Dynamic Stop Tightening: 1.0 * ATR if explosive volume and expanding MACD
            dynamic_mult = 1.0 if (vol_z > 2.0 and hist_now > hist_prev) else p["atr_stop_mult"]
            stop = close - dynamic_mult * atr
            self._consume_event(ctx, symbol, "long", current_time)
            mover_tag = f" [top_gainer +{ctx.mover_change_pct:.1f}%]" if bias == "gainer" else ""
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=confidence,
                strategy=self.name,
                reason=f"EMA9>EMA20 above VWAP, MACD event confirmed, RSI {rsi:.0f}{mover_tag}",
                price=close,
                stop_loss=stop,
                take_profit=None,
            )

        # ---- Short candidate ----
        if (
            allow_short
            and last["ema9"] < last["ema20"]
            and close < float(last["vwap"])
            and (100.0 - p["rsi_max"]) <= rsi <= (100.0 - p["rsi_min"])
            and self._valid_event(ctx, symbol, "short", current_bar_idx, df)
        ):
            # Dynamic Stop Tightening: 1.0 * ATR if explosive volume and expanding MACD
            dynamic_mult = 1.0 if (vol_z > 2.0 and hist_now < hist_prev) else p["atr_stop_mult"]
            stop = close + dynamic_mult * atr
            self._consume_event(ctx, symbol, "short", current_time)
            mover_tag = f" [top_loser {ctx.mover_change_pct:.1f}%]" if bias == "loser" else ""
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=confidence,
                strategy=self.name,
                reason=f"EMA9<EMA20 below VWAP, MACD event confirmed, RSI {rsi:.0f}{mover_tag}",
                price=close,
                stop_loss=stop,
                take_profit=None,
            )

        return None
