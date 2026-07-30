"""VWAP mean reversion.

In non-trending tape, price stretched far from session VWAP tends to snap
back. Enter against the stretch when RSI(2) confirms exhaustion; target the
VWAP itself, stop beyond the stretch extreme.
"""

from __future__ import annotations

import pandas as pd

from app.core.domain import Signal, SignalAction
from app.indicators.ta import rolling_zscore
from app.strategies.base import Strategy, StrategyContext, register


@register
class VwapReversion(Strategy):
    name = "vwap_reversion"
    description = "Fade extreme stretches from session VWAP in range-bound tape"
    default_params = {
        "z_entry": 2.0,          # |z| of vwap distance to trigger
        "z_window": 60,
        "er_max": 0.30,          # only in non-trending conditions
        "rsi2_oversold": 10.0,
        "rsi2_overbought": 90.0,
        "atr_stop_mult": 2.0,
        "min_target_atr": 0.5,   # skip when VWAP is too close to matter
        "min_reward_risk": 1.0,  # economic filter: reward/risk must be >= 1.0
        "max_vwap_drift_pct": 0.15,
        "use_ema_filter": True,
        "min_minutes_since_open": 30,
        "trailing_stop_atr_mult": 1.5,
    }

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        p = self.params
        if ctx.position is not None or len(df) < p["z_window"] + 5:
            return None

        # Block entries in the first N minutes of the session (opening drive protection)
        from zoneinfo import ZoneInfo
        now_et = ctx.now.astimezone(ZoneInfo("America/New_York"))
        minutes_since_open = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
        if minutes_since_open < p.get("min_minutes_since_open", 30):
            return None
        last = df.iloc[-1]
        needed = ("vwap_dist_pct", "er20", "rsi2", "atr14", "vwap")
        if any(pd.isna(last.get(c)) for c in needed):
            return None

        er = float(last["er20"])
        if er > p["er_max"]:
            return None

        z_series = rolling_zscore(df["vwap_dist_pct"], int(p["z_window"]))
        z = float(z_series.iloc[-1]) if pd.notna(z_series.iloc[-1]) else 0.0
        close = float(last["close"])
        vwap = float(last["vwap"])
        atr = float(last["atr14"])
        rsi2 = float(last["rsi2"])
        if atr <= 0:
            return None

        confidence = min(0.85, 0.5 + 0.1 * (abs(z) - p["z_entry"]) + 0.1)

        # Check VWAP slope over last 30 bars to avoid catching falling knives or shorting runaway trains
        max_drift = p.get("max_vwap_drift_pct", 0.15)
        if len(df) >= 30 and max_drift > 0:
            vwap_change_pct = (vwap - float(df["vwap"].iloc[-30])) / vwap * 100.0
            if z <= -p["z_entry"] and vwap_change_pct < -max_drift:
                return None
            if z >= p["z_entry"] and vwap_change_pct > max_drift:
                return None

        # Stacked EMA Trend Filter: avoid buying bearish stacks or shorting bullish stacks
        if p.get("use_ema_filter", True):
            ema9 = float(last.get("ema9", 0.0))
            ema20 = float(last.get("ema20", 0.0))
            ema50 = float(last.get("ema50", 0.0))
            if ema9 > 0 and ema20 > 0 and ema50 > 0:
                is_bullish_stack = ema9 > ema20 > ema50
                is_bearish_stack = ema9 < ema20 < ema50
                if z <= -p["z_entry"] and is_bearish_stack:
                    return None
                if z >= p["z_entry"] and is_bullish_stack:
                    return None
                    


        if z <= -p["z_entry"] and rsi2 <= p["rsi2_oversold"] and close < vwap:
            if (vwap - close) < p["min_target_atr"] * atr:
                return None
            risk = p["atr_stop_mult"] * atr
            reward = vwap - close
            if risk > 0 and (reward / risk) < p.get("min_reward_risk", 1.0):
                return None
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=confidence,
                strategy=self.name,
                reason=f"Stretched {z:.1f} sigma below VWAP with RSI(2) {rsi2:.0f}",
                price=close,
                stop_loss=close - risk,
                take_profit=vwap,
            )
        if z >= p["z_entry"] and rsi2 >= p["rsi2_overbought"] and close > vwap:
            if (close - vwap) < p["min_target_atr"] * atr:
                return None
            risk = p["atr_stop_mult"] * atr
            reward = close - vwap
            if risk > 0 and (reward / risk) < p.get("min_reward_risk", 1.0):
                return None
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=confidence,
                strategy=self.name,
                reason=f"Stretched {z:.1f} sigma above VWAP with RSI(2) {rsi2:.0f}",
                price=close,
                stop_loss=close + risk,
                take_profit=vwap,
            )
        return None
