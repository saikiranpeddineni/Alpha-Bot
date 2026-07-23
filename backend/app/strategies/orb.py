"""Opening Range Breakout (ORB).

Classic intraday momentum entry: after the first N minutes establish the
opening range, trade the first volume-confirmed breakout in either
direction. Stops anchor to the opposite side of the range (ATR-capped),
targets at a fixed reward:risk. One attempt per symbol per session.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
from zoneinfo import ZoneInfo

from app.core.domain import Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class OpeningRangeBreakout(Strategy):
    name = "orb"
    description = "Opening range breakout with volume confirmation (first 15 min range)"
    default_params = {
        "range_minutes": 15,
        "entry_cutoff_minute": 180,   # no fresh breakout entries after 12:30 ET
        "breakout_buffer_bps": 5,     # close must clear the range by 0.05%
        "atr_buffer_mult": 0.0,      # if > 0, overrides breakout_buffer_bps with (mult * ATR)
        "volume_z_min": 0.5,
        "atr_stop_mult": 2.0,         # risk cap when the range is very wide
        "reward_risk": 2.0,
        "trailing_stop_atr_mult": 0.0 # if > 0, activates dynamic trailing stop
    }

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        super().__init__(params, enabled)
        self._traded: set[tuple[str, object]] = set()
        self._pending: dict[tuple[str, object], datetime] = {}

    def reset(self) -> None:
        self._traded.clear()
        self._pending.clear()

    def on_entry_filled(self, symbol: str, timestamp: object) -> None:
        dt = getattr(timestamp, "date", lambda: timestamp)()
        key = (symbol, dt)
        self._pending.pop(key, None)
        self._traded.add(key)

    def on_entry_rejected(self, symbol: str, timestamp: object) -> None:
        dt = getattr(timestamp, "date", lambda: timestamp)()
        key = (symbol, dt)
        self._pending.pop(key, None)

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        p = self.params
        if ctx.position is not None or df.empty:
            return None

        et_dates = df.index.tz_convert(ET).date
        today = et_dates[-1]
        key = (symbol, today)
        if key in self._traded:
            return None

        pending_time = self._pending.get(key)
        if pending_time is not None:
            # Auto-expire stale pending entries older than 5 minutes
            if (ctx.now - pending_time) < timedelta(minutes=5):
                return None
            self._pending.pop(key, None)

        today_df = df[et_dates == today]
        n_today = len(today_df)
        rng_n = int(p["range_minutes"])
        if n_today <= rng_n or n_today > int(p["entry_cutoff_minute"]):
            return None

        or_high = float(today_df["high"].iloc[:rng_n].max())
        or_low = float(today_df["low"].iloc[:rng_n].min())
        last = df.iloc[-1]
        close = float(last["close"])
        atr = float(last["atr14"]) if pd.notna(last.get("atr14")) else None
        vol_z = float(last["vol_z"]) if pd.notna(last.get("vol_z")) else 0.0
        if atr is None or atr <= 0:
            return None

        if p.get("atr_buffer_mult", 0.0) > 0.0:
            buffer = 1.0 + (p["atr_buffer_mult"] * atr / close)
        else:
            buffer = 1.0 + p["breakout_buffer_bps"] / 10_000.0
        volume_ok = vol_z >= p["volume_z_min"]
        confidence = min(0.85, 0.55 + 0.08 * max(0.0, vol_z))

        if close > or_high * buffer and volume_ok:
            stop = max(or_low, close - p["atr_stop_mult"] * atr)
            risk = close - stop
            if risk <= 0:
                return None
            self._pending[key] = ctx.now
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=confidence,
                strategy=self.name,
                reason=f"Broke above {rng_n}m opening range {or_high:.2f} (vol z {vol_z:.1f})",
                price=close,
                stop_loss=stop,
                take_profit=close + p["reward_risk"] * risk,
            )
        if close < or_low / buffer and volume_ok:
            stop = min(or_high, close + p["atr_stop_mult"] * atr)
            risk = stop - close
            if risk <= 0:
                return None
            self._pending[key] = ctx.now
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=confidence,
                strategy=self.name,
                reason=f"Broke below {rng_n}m opening range {or_low:.2f} (vol z {vol_z:.1f})",
                price=close,
                stop_loss=stop,
                take_profit=close - p["reward_risk"] * risk,
            )
        return None
