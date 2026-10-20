"""Bollinger Band Squeeze Strategy.

Enters on volume-confirmed breakouts from low-volatility consolidation periods (squeezes).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

EVENT_WINDOW_BARS = 3  # short confirmation window

@register
class BollingerBandSqueeze(Strategy):
    name = "bb_squeeze"
    description = "Volume-confirmed breakout from a Bollinger Band squeeze"

    is_breakout = True
    is_structural_breakout = True
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
        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-04 — BB Squeeze min_minutes and new gates
        # ORIGINAL INTENT: min_minutes_since_open=5; open-auction excluded for 5 bars.
        # REASON FOR CHANGE: 5 minutes is not enough for a reliable VWAP/BB context.
        #   Squeezes need an established intraday range (>=15 min) for the band width
        #   percentile to be meaningful. Also adding:
        #   - breakout_buffer_atr: bar must close >= bb_upper + 0.2*ATR to kill head-fake wick tags.
        #   - volatile_squeeze_pct: VOLATILE regime allowed only when true extreme squeeze (<=0.10 pct) AND vol>=1.5.
        #   - min_target_atr: economic gate — skip when target < 0.5*ATR (uneconomic R).
        # SAFE ROLLBACK: Set min_minutes_since_open=5, breakout_buffer_atr=0.0,
        #   volatile_squeeze_pct=None, min_target_atr=0.0
        # [NEW CODE]:
        "min_minutes_since_open": 15,          # CR-04: need established VWAP range
        "event_window_bars": EVENT_WINDOW_BARS,
        "breakout_buffer_atr": 0.2,            # CR-04: kill head-fake wick tags (must clear bb_upper + 0.2*ATR)
        "volatile_squeeze_pct": 0.10,          # CR-04: allow VOLATILE only if width_pct <= this extreme
        "volatile_min_vol_z": 1.5,             # CR-04: allow VOLATILE only if vol_z >= this threshold
        "min_target_atr": 0.5,                 # CR-04: economic gate (target < 0.5*ATR = skip)
        # =============================================================================
    }

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        super().__init__(params, enabled)
        # {symbol: {"long": {"time": datetime, "emitted": bool}, "short": ...}}
        self._events: dict[str, dict[str, dict]] = {}

    def reset(self) -> None:
        self._events.clear()

    def _set_event(self, symbol: str, side: str, event_time: datetime) -> None:
        if symbol not in self._events:
            self._events[symbol] = {}
        self._events[symbol][side] = {"time": event_time, "emitted": False}

    def _check_and_consume_event(self, symbol: str, side: str, df: pd.DataFrame, window: int) -> bool:
        """Timestamp-based confirmation.

        Positional indices (``len(df)``) are constant under fixed-length rolling
        windows, which permanently froze events at ``bars_elapsed == 0`` — the
        strategy could never confirm or expire an event. Elapsed bars are now
        derived from the event timestamp's location inside the current frame.
        """
        if symbol not in self._events or side not in self._events[symbol]:
            return False

        event = self._events[symbol][side]
        if event["emitted"]:
            return False

        try:
            event_idx = df.index.get_loc(event["time"])
        except KeyError:
            # Event rolled out of the frame without confirming.
            self._events[symbol].pop(side, None)
            return False

        bars_elapsed = (len(df) - 1) - event_idx
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

            # Anchor events to the decision bar's own timestamp so the event can
            # be located inside future (possibly fixed-length) frames.
            event_ts = df.index[-1].to_pydatetime()
            if crossed_up and vol_z >= p["vol_z_min"]:
                self._set_event(symbol, "long", event_ts)
            if crossed_down and vol_z >= p["vol_z_min"]:
                self._set_event(symbol, "short", event_ts)

        # Check for confirmed event
        confirmed_long = self._check_and_consume_event(symbol, "long", df, window)
        confirmed_short = self._check_and_consume_event(symbol, "short", df, window)

        if not confirmed_long and not confirmed_short:
            return None

        # Ensure the breakout is still valid and hasn't totally collapsed back into the band
        if confirmed_long and close < last["bb_mid"]:
            return None
        if confirmed_short and close > last["bb_mid"]:
            return None

        # =============================================================================
        # [ADDED 2026-10-14]: CR-04 — breakout_buffer_atr head-fake kill
        # ORIGINAL INTENT: Only check close vs bb_mid; any close above bb_upper counted.
        # REASON FOR CHANGE: Wick tags at exactly bb_upper trigger false confirmations.
        #   Requiring close >= bb_upper + 0.2*ATR ensures the breakout bar body has
        #   genuinely cleared the band, not just poked it with a wick.
        # SAFE ROLLBACK: Set breakout_buffer_atr=0.0
        bb_upper = float(last["bb_upper"])
        bb_lower = float(last["bb_lower"])
        buf_atr = float(p.get("breakout_buffer_atr", 0.2)) * atr
        if buf_atr > 0:
            if confirmed_long and close < (bb_upper + buf_atr):
                return None
            if confirmed_short and close > (bb_lower - buf_atr):
                return None
        # =============================================================================

        # Structural VWAP vs EMA20 trend filter (sibling-proven anchor) to
        # filter out false breakouts (head-fakes). After the first 30 minutes
        # the EMA20/session-VWAP relationship is reliable; before that, both
        # are still catching up to the gap, so structural checks are skipped.
        if p.get("use_trend_filter", True):
            ema20 = float(last.get("ema20", 0.0))
            vwap_px = float(last.get("vwap", 0.0))
            if ema20 > 0 and vwap_px > 0 and mins_since_open > 30:
                structural_uptrend = ema20 > vwap_px
                structural_downtrend = ema20 < vwap_px
                if confirmed_long and structural_downtrend:
                    return None
                if confirmed_short and structural_uptrend:
                    return None

            from app.core.domain import Regime
            # =============================================================================
            # [MODIFIED 2026-10-14]: CR-04 — Intelligent VOLATILE regime handling for extreme squeezes
            # ORIGINAL INTENT: Blanket VOLATILE reject regardless of squeeze quality.
            # REASON FOR CHANGE: An extreme squeeze (width_pct <= volatile_squeeze_pct=0.10)
            #   in a VOLATILE regime represents the coil before the spring — this is exactly
            #   when BB squeeze has highest expected value. Blanket rejection throws away
            #   the highest-quality setups. Allow when: extreme squeeze AND vol_z >= 1.5.
            # SAFE ROLLBACK: Remove the condition; keep the original blanket VOLATILE reject.
            # [NEW CODE]:
            if ctx.regime == Regime.VOLATILE:
                volatile_squeeze_pct = float(p.get("volatile_squeeze_pct", 0.10))
                # Compute the current bar's width percentile
                w_series = df["bb_width"].iloc[:-1].dropna()
                lb = int(p["width_percentile_lookback"])
                current_width_pct = (w_series.iloc[-lb:] <= float(last["bb_width"])).mean() if len(w_series) >= lb // 2 else 1.0
                is_extreme_squeeze = current_width_pct <= volatile_squeeze_pct
                # [NEW CODE]:
                volatile_min_vol = float(p.get("volatile_min_vol_z", 1.5))
                high_vol = vol_z >= volatile_min_vol
                if not (is_extreme_squeeze and high_vol):
                    return None  # Still reject non-extreme VOLATILE
            # =============================================================================

        confidence = min(0.85, 0.55 + 0.08 * max(0.0, vol_z))

        if confirmed_long:
            stop = close - p["atr_stop_mult"] * atr
            risk = close - stop
            target = close + p["reward_risk"] * risk
            # =============================================================================
            # [ADDED 2026-10-14]: CR-04 — Economic gate (min_target_atr)
            # ORIGINAL INTENT: No target-size sanity check; all confirmed breakouts emitted.
            # REASON FOR CHANGE: When ATR is very small, the reward (target - entry) can be
            #   sub-ATR, making the trade uneconomic relative to spread + slippage costs.
            #   Skip when target - entry < 0.5 * ATR (mirrors vwap_reversion.py:100-105).
            # SAFE ROLLBACK: Set min_target_atr=0.0
            min_t_atr = float(p.get("min_target_atr", 0.5))
            if min_t_atr > 0 and risk > 0 and (target - close) < min_t_atr * atr:
                return None
            # =============================================================================
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=confidence,
                strategy=self.name,
                reason=f"BB Squeeze breakout up confirmed (vol z {vol_z:.1f})",
                price=close,
                stop_loss=stop,
                take_profit=target,
            )

        if confirmed_short:
            stop = close + p["atr_stop_mult"] * atr
            risk = stop - close
            target = close - p["reward_risk"] * risk
            # Economic gate (same as long)
            min_t_atr = float(p.get("min_target_atr", 0.5))
            if min_t_atr > 0 and risk > 0 and (close - target) < min_t_atr * atr:
                return None
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=confidence,
                strategy=self.name,
                reason=f"BB Squeeze breakout down confirmed (vol z {vol_z:.1f})",
                price=close,
                stop_loss=stop,
                take_profit=target,
            )

        return None
