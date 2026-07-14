"""RSI(2) dip-buy reversion (Connors-style, adapted intraday).

Buy brief panic dips inside an intact intraday uptrend: RSI(2) at an
extreme low while price holds above a rising EMA50. Exits on RSI(2)
normalization or the protective bracket. Long-only by design -- upward
drift makes the short side structurally weaker.
"""

from __future__ import annotations

import pandas as pd

from app.core.domain import Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register


@register
class Rsi2Reversion(Strategy):
    name = "rsi2_reversion"
    description = "Buy RSI(2) panic dips inside an intact intraday uptrend"
    default_params = {
        "rsi2_entry": 5.0,
        "rsi2_exit": 70.0,
        "ema_slope_lookback": 15,
        "atr_stop_mult": 1.5,
        "reward_risk": 1.5,
        "enable_shorts": False,         # if True, sells RSI(2) rips below falling EMA50
        "trailing_stop_atr_mult": 0.0,  # if > 0, activates dynamic trailing stop
    }

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        p = self.params
        if len(df) < 70:
            return None
        last = df.iloc[-1]
        if any(pd.isna(last.get(c)) for c in ("rsi2", "ema50", "atr14")):
            return None
        close = float(last["close"])
        rsi2 = float(last["rsi2"])

        if ctx.position is not None:
            if ctx.position.strategy == self.name:
                if ctx.position.qty > 0 and rsi2 >= p["rsi2_exit"]:
                    return Signal(
                        symbol=symbol,
                        action=SignalAction.EXIT,
                        confidence=0.9,
                        strategy=self.name,
                        reason=f"RSI(2) normalized to {rsi2:.0f}",
                        price=close,
                    )
                if ctx.position.qty < 0 and p.get("enable_shorts", False) and rsi2 <= (100.0 - p["rsi2_exit"]):
                    return Signal(
                        symbol=symbol,
                        action=SignalAction.EXIT,
                        confidence=0.9,
                        strategy=self.name,
                        reason=f"RSI(2) normalized to {rsi2:.0f}",
                        price=close,
                    )
            return None

        ema50 = df["ema50"]
        lb = int(p["ema_slope_lookback"])
        if len(ema50) <= lb or pd.isna(ema50.iloc[-1 - lb]):
            return None
        ema_rising = float(ema50.iloc[-1]) > float(ema50.iloc[-1 - lb])
        ema_falling = float(ema50.iloc[-1]) < float(ema50.iloc[-1 - lb])
        atr = float(last["atr14"])
        if atr <= 0:
            return None

        if rsi2 <= p["rsi2_entry"] and close > float(ema50.iloc[-1]) and ema_rising:
            stop = close - p["atr_stop_mult"] * atr
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=min(0.8, 0.55 + (p["rsi2_entry"] - rsi2) * 0.04),
                strategy=self.name,
                reason=f"RSI(2) {rsi2:.1f} dip above rising EMA50",
                price=close,
                stop_loss=stop,
                take_profit=close + p["reward_risk"] * (close - stop),
            )

        if p.get("enable_shorts", False) and rsi2 >= (100.0 - p["rsi2_entry"]) and close < float(ema50.iloc[-1]) and ema_falling:
            stop = close + p["atr_stop_mult"] * atr
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=min(0.8, 0.55 + (rsi2 - (100.0 - p["rsi2_entry"])) * 0.04),
                strategy=self.name,
                reason=f"RSI(2) {rsi2:.1f} rip below falling EMA50",
                price=close,
                stop_loss=stop,
                take_profit=close - p["reward_risk"] * (stop - close),
            )

        return None
