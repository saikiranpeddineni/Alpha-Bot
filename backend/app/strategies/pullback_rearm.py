"""Pullback re-arm strategy (CR-02b).

Second-chance continuation entry for high-conviction movers that were
initially rejected by session-extreme or signal-bar chase vetoes.
Instead of loosening the veto, this strategy waits for the move to digest:
holding near highs/lows + multi-bar pullback + reclaiming EMA9 with volume.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

logger = logging.getLogger("alphadesk")


@register
class PullbackRearm(Strategy):
    name = "pullback_rearm"
    description = "Pullback re-arm continuation entry following extreme/chase exhaustion"

    is_breakout = False
    is_structural_breakout = False
    is_mean_reversion = False
    ml_threshold = 0.53
    evaluation_horizons = ["30m", "60m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.20,
        Regime.TREND_DOWN: 1.20,
        Regime.RANGE: 0.70,
        Regime.VOLATILE: 0.85,
    }

    default_params = {
        "rearm_lookback_bars": 30,
        "rearm_vol_z_min": 1.0,
        "min_pullback_bars": 3,
        "atr_stop_mult": 2.0,
        "reward_risk": 2.0,
        "trailing_stop_atr_mult": 1.5,
        "min_minutes_since_open": 15,
        "max_entries_per_day": 1,
        "min_target_atr": 0.5,
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
        p = self.params
        if len(df) < 35:
            return None

        # Exit management handled by PIS
        if ctx.position is not None:
            return None

        now_et = ctx.now.astimezone(ZoneInfo("America/New_York"))
        mins_et = now_et.hour * 60 + now_et.minute
        minutes_from_open = mins_et - (9 * 60 + 30)
        if minutes_from_open < int(p.get("min_minutes_since_open", 15)):
            return None

        today_date = now_et.date()
        if p.get("max_entries_per_day", 1) > 0 and (symbol, today_date) in self._traded:
            return None

        last = df.iloc[-1]
        needed = ("close", "open", "high", "low", "ema9", "ema20", "vwap", "atr14", "vol_z")
        if any(pd.isna(last.get(c)) for c in needed):
            return None

        close = float(last["close"])
        ema9 = float(last["ema9"])
        ema20 = float(last["ema20"])
        vwap = float(last["vwap"])
        atr = float(last["atr14"])
        vol_z = float(last["vol_z"])

        if atr <= 0 or vol_z < float(p.get("rearm_vol_z_min", 1.0)):
            return None

        lookback = int(p.get("rearm_lookback_bars", 30))
        recent_df = df.iloc[-lookback:]
        if len(recent_df) < 10:
            return None

        # ---- Long Pullback Re-Arm ----
        # 1. Macro / structural alignment: EMA20 > VWAP and close above daily SMA20 if known
        long_aligned = ema20 >= (vwap - 0.2 * atr) and close > vwap
        if ctx.daily_sma20 is not None and close < ctx.daily_sma20:
            long_aligned = False

        if long_aligned and (ctx.mover_bias != "loser"):
            # 2. Reclaim: Prior bars dipped below EMA9, current bar reclaims above EMA9
            prev_bars = df.iloc[-4:-1]
            had_pullback = (prev_bars["close"] <= prev_bars["ema9"]).any() or (prev_bars["low"] <= prev_bars["ema20"]).any()
            reclaimed_ema9 = close > ema9 and df["close"].iloc[-2] <= df["ema9"].iloc[-2]

            # 3. Held structure: Low of pullback did not break below EMA20 - 1.0*ATR
            recent_low = float(prev_bars["low"].min())
            held_structure = recent_low >= (ema20 - 1.0 * atr)

            if had_pullback and reclaimed_ema9 and held_structure:
                stop = round(min(recent_low - 0.2 * atr, close - float(p.get("atr_stop_mult", 2.0)) * atr), 4)
                risk_dist = close - stop
                min_target_atr = float(p.get("min_target_atr", 0.5))
                if risk_dist >= min_target_atr * atr:
                    target = round(close + float(p.get("reward_risk", 2.0)) * risk_dist, 4)
                    confidence = min(0.85, 0.60 + 0.05 * min(vol_z, 2.0))
                    return Signal(
                        symbol=symbol,
                        action=SignalAction.ENTER_LONG,
                        confidence=confidence,
                        strategy=self.name,
                        reason=f"Pullback re-arm: reclaimed EMA9 after {len(prev_bars)}-bar dip (vol_z {vol_z:.1f})",
                        price=close,
                        stop_loss=stop,
                        take_profit=target,
                    )

        # ---- Short Pullback Re-Arm ----
        short_aligned = ema20 <= (vwap + 0.2 * atr) and close < vwap
        if ctx.daily_sma20 is not None and close > ctx.daily_sma20:
            short_aligned = False

        if short_aligned and (ctx.mover_bias != "gainer"):
            prev_bars = df.iloc[-4:-1]
            had_pullback = (prev_bars["close"] >= prev_bars["ema9"]).any() or (prev_bars["high"] >= prev_bars["ema20"]).any()
            reclaimed_ema9 = close < ema9 and df["close"].iloc[-2] >= df["ema9"].iloc[-2]

            recent_high = float(prev_bars["high"].max())
            held_structure = recent_high <= (ema20 + 1.0 * atr)

            if had_pullback and reclaimed_ema9 and held_structure:
                stop = round(max(recent_high + 0.2 * atr, close + float(p.get("atr_stop_mult", 2.0)) * atr), 4)
                risk_dist = stop - close
                min_target_atr = float(p.get("min_target_atr", 0.5))
                if risk_dist >= min_target_atr * atr:
                    target = round(close - float(p.get("reward_risk", 2.0)) * risk_dist, 4)
                    confidence = min(0.85, 0.60 + 0.05 * min(vol_z, 2.0))
                    return Signal(
                        symbol=symbol,
                        action=SignalAction.ENTER_SHORT,
                        confidence=confidence,
                        strategy=self.name,
                        reason=f"Pullback re-arm short: rejected EMA9 after {len(prev_bars)}-bar bounce (vol_z {vol_z:.1f})",
                        price=close,
                        stop_loss=stop,
                        take_profit=target,
                    )

        return None
