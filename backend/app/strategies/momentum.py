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
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register
from app.strategies.entry_quality import evaluate_entry_quality
from app.strategies.opportunity_state import classify_opportunity, OpportunityState

logger = logging.getLogger("alphadesk")

EVENT_WINDOW_BARS = 10  # max bars a pending MACD event stays valid


@register
class MomentumCross(Strategy):
    name = "momentum"
    description = "EMA crossover momentum with MACD inflection pending event"

    is_breakout = True
    ml_threshold = 0.55
    evaluation_horizons = ["60m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.20, Regime.TREND_DOWN: 1.05, Regime.RANGE: 0.60, Regime.VOLATILE: 0.80
    }

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
        "min_minutes_since_open": 5,
        "max_vwap_extension_pct": 1.75,
        "hard_vwap_extension_pct": 3.00,
        "max_atr_extension": 1.25,
        "hard_atr_extension": 1.80,
        "min_entry_quality_score": 70.0,
        "min_structure_score": 60.0,
        "allow_price_discovery_entries": False,
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
        now_et = ctx.now.astimezone(ZoneInfo("America/New_York"))
        mins_et = now_et.hour * 60 + now_et.minute

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

        # Trend score approximation based on EMAs and HTF
        trend_score = 75.0 if crossed_up or crossed_down else 50.0

        # Mover and news contexts
        is_news = getattr(ctx, "news_direction", None) is not None
        catalyst_score = 100.0 if is_news else 50.0

        # Calculate opportunity state
        extension_atr = abs(close - float(last["vwap"])) / max(atr, 1e-9)
        atr_pct = (atr / close) * 100.0
        
        opportunity_state = classify_opportunity(
            atr_pct=atr_pct,
            gap_pct=0.0,  # gap tracking handled in engine_evaluation
            news_catalyst=is_news,
            volume_z=vol_z,
            extension_atr=extension_atr,
            trend_score=trend_score,
        )

        if opportunity_state == OpportunityState.PRICE_DISCOVERY:
            if not p.get("allow_price_discovery_entries", False):
                self._debug(ctx, f"[momentum] PRICE_DISCOVERY_BLOCKED sym={symbol}")
                return None
                
        # Required thresholds
        if opportunity_state == OpportunityState.EVENT:
            required_entry_quality = 78.0
        else:
            required_entry_quality = p.get("min_entry_quality_score", 70.0)

        # ---- Long candidate ----
        if (
            allow_long
            and last["ema9"] > last["ema20"]
            and close > float(last["vwap"])
            and p["rsi_min"] <= rsi <= p["rsi_max"]
            and self._valid_event(ctx, symbol, "long", current_bar_idx, df)
        ):
            entry_quality = evaluate_entry_quality(
                side="long",
                close=close,
                vwap=float(last["vwap"]),
                atr=atr,
                df=df,
                vol_z=vol_z,
                catalyst_score=catalyst_score,
                trend_score=trend_score,
            )
            
            if entry_quality.score < required_entry_quality or not entry_quality.approved:
                self._debug(
                    ctx,
                    f"[momentum] ENTRY_REJECTED sym={symbol} "
                    f"score={entry_quality.score:.1f} (req={required_entry_quality}) "
                    f"reasons={entry_quality.reasons}"
                )
                # Keep event alive if it's just a poor location, but consume if structural failure?
                # The user mandate just says return None.
                return None
                
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
                reason=f"EMA9>EMA20 above VWAP, MACD event, RSI {rsi:.0f}{mover_tag}, EQ={entry_quality.score:.1f}",
                price=close,
                stop_loss=stop,
                take_profit=None,
                strategy_params={
                    "entry_quality_score": entry_quality.score,
                    "entry_quality": entry_quality.quality.value,
                    "location_score": entry_quality.location_score,
                    "extension_score": entry_quality.extension_score,
                    "structure_score": entry_quality.structure_score,
                    "opportunity_state": opportunity_state.value,
                    "opportunity_score": trend_score,
                    "vwap_distance_pct": abs(close - float(last["vwap"])) / float(last["vwap"]) * 100.0,
                    "atr_extension": extension_atr,
                }
            )

        # ---- Short candidate ----
        if (
            allow_short
            and last["ema9"] < last["ema20"]
            and close < float(last["vwap"])
            and (100.0 - p["rsi_max"]) <= rsi <= (100.0 - p["rsi_min"])
            and self._valid_event(ctx, symbol, "short", current_bar_idx, df)
        ):
            entry_quality = evaluate_entry_quality(
                side="short",
                close=close,
                vwap=float(last["vwap"]),
                atr=atr,
                df=df,
                vol_z=vol_z,
                catalyst_score=catalyst_score,
                trend_score=trend_score,
            )
            
            if entry_quality.score < required_entry_quality or not entry_quality.approved:
                self._debug(
                    ctx,
                    f"[momentum] ENTRY_REJECTED sym={symbol} "
                    f"score={entry_quality.score:.1f} (req={required_entry_quality}) "
                    f"reasons={entry_quality.reasons}"
                )
                return None
                
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
                reason=f"EMA9<EMA20 below VWAP, MACD event, RSI {rsi:.0f}{mover_tag}, EQ={entry_quality.score:.1f}",
                price=close,
                stop_loss=stop,
                take_profit=None,
                strategy_params={
                    "entry_quality_score": entry_quality.score,
                    "entry_quality": entry_quality.quality.value,
                    "location_score": entry_quality.location_score,
                    "extension_score": entry_quality.extension_score,
                    "structure_score": entry_quality.structure_score,
                    "opportunity_state": opportunity_state.value,
                    "opportunity_score": trend_score,
                    "vwap_distance_pct": abs(close - float(last["vwap"])) / float(last["vwap"]) * 100.0,
                    "atr_extension": extension_atr,
                }
            )

        return None
