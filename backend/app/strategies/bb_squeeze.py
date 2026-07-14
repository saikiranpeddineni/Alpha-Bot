"""Bollinger Band Squeeze Strategy.

Enters on volume-confirmed breakouts from low-volatility consolidation periods (squeezes).
"""

from __future__ import annotations

import pandas as pd

from app.core.domain import Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register


@register
class BollingerBandSqueeze(Strategy):
    name = "bb_squeeze"
    description = "Volume-confirmed breakout from a Bollinger Band squeeze"
    default_params = {
        "width_percentile_lookback": 100,
        "width_percentile_threshold": 0.15,
        "vol_z_min": 1.0,
        "atr_stop_mult": 2.0,
        "reward_risk": 2.0,
        "trailing_stop_atr_mult": 4.0,
        "use_trend_filter": True,
    }

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        p = self.params
        lb = int(p["width_percentile_lookback"])
        if len(df) < lb + 5:
            return None

        last, prev = df.iloc[-1], df.iloc[-2]
        needed = ("bb_width", "bb_upper", "bb_lower", "vol_z", "atr14")
        if any(pd.isna(last.get(c)) for c in needed) or any(pd.isna(prev.get(c)) for c in ("bb_upper", "bb_lower")):
            return None

        # Check if squeezed within the last 5 bars (consolidation phase)
        width_series = df["bb_width"]
        squeeze_active = False
        for i in range(-5, 0):
            window = width_series.iloc[i - lb : i].dropna()
            if len(window) < lb // 2:
                continue
            val = width_series.iloc[i]
            pct = (window <= val).mean()
            if pct <= p["width_percentile_threshold"]:
                squeeze_active = True
                break

        if not squeeze_active:
            return None

        close = float(last["close"])
        vol_z = float(last["vol_z"])
        atr = float(last["atr14"])
        if atr <= 0 or vol_z < p["vol_z_min"]:
            return None

        crossed_up = prev["close"] <= prev["bb_upper"] and close > last["bb_upper"]
        crossed_down = prev["close"] >= prev["bb_lower"] and close < last["bb_lower"]

        # Check EMA trend alignment and AI market regime to filter out false breakouts (head-fakes)
        if p.get("use_trend_filter", True):
            ema20 = float(last.get("ema20", 0.0))
            ema50 = float(last.get("ema50", 0.0))
            if ema20 > 0 and ema50 > 0:
                if crossed_up and ema20 < ema50:
                    # Block long breakout if medium-term trend is bearish
                    return None
                if crossed_down and ema20 > ema50:
                    # Block short breakout if medium-term trend is bullish
                    return None

            # Block breakout trades that fight the active AI-detected regime
            from app.core.domain import Regime
            if ctx.regime == Regime.VOLATILE:
                # Whipsaw protection: block breakouts in highly volatile/choppy market regimes
                return None

        confidence = min(0.85, 0.55 + 0.08 * max(0.0, vol_z))

        if ctx.position is not None:
            return None

        if crossed_up:
            stop = close - p["atr_stop_mult"] * atr
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=confidence,
                strategy=self.name,
                reason=f"BB Squeeze breakout up (vol z {vol_z:.1f})",
                price=close,
                stop_loss=stop,
                take_profit=close + p["reward_risk"] * (close - stop),
            )
        if crossed_down:
            stop = close + p["atr_stop_mult"] * atr
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=confidence,
                strategy=self.name,
                reason=f"BB Squeeze breakout down (vol z {vol_z:.1f})",
                price=close,
                stop_loss=stop,
                take_profit=close - p["reward_risk"] * (stop - close),
            )
        return None
