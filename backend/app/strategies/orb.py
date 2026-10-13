"""Opening Range Breakout (ORB).

Classic intraday momentum entry: after the first N minutes establish the
opening range, trade the first volume-confirmed breakout in either
direction. Stops anchor to the opposite side of the range (ATR-capped),
targets at a fixed reward:risk. One attempt per symbol per session.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.data.base import resample_bars
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class OpeningRangeBreakout(Strategy):
    name = "orb"
    description = "Trades breakouts of the opening N-minute range"

    is_breakout = True
    is_structural_breakout = True
    ml_threshold = 0.55
    evaluation_horizons = ["60m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.15, Regime.TREND_DOWN: 1.15, Regime.RANGE: 0.70, Regime.VOLATILE: 1.00
    }
    default_params = {
        "range_minutes": 15,
        "breakout_buffer_bps": 5,     # close must clear the range by 0.05%
        "atr_buffer_mult": 0.0,       # if > 0, overrides breakout_buffer_bps with (mult * ATR)
        "volume_z_min": 1.5,
        "atr_stop_mult": 2.0,         # risk cap when the range is very wide
        "reward_risk": 2.0,
        "trailing_stop_atr_mult": 1.5, # if > 0, activates dynamic trailing stop
        "mft_alignment": False,       # optional 5m higher-timeframe trend alignment (set True in config)
        "use_daily_drift_filter": True,  # fade-proofing: don't fight the 20-day trend
        "drift_buffer_pct": 1.0,         # +/- % around daily SMA20 that counts as neutral
        "min_risk_atr": 0.75,            # skip entries whose stop sits unrealistically tight
        "min_acceptance_ratio": 0.50,    # breakout bar must CLOSE in its directional
                                         # extreme (body-dominant = accepted breakout,
                                         # not a wick fake-out). Sibling-proven gate.
        "climax_atr_mult": 2.5,          # relaxed from 1.5: breakouts legitimately detach
                                         # from EMA9; acceptance+drift now carry quality.
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

    def _check_5m_trend(self, df: pd.DataFrame) -> tuple[bool, bool]:
        """Check 5-minute trend direction (bullish, bearish) from 1-minute dataframe."""
        if len(df) < 50:
            return True, True
        try:
            df_5m = resample_bars(df.tail(200), "5Min")
            if len(df_5m) < 10:
                return True, True
            ema20 = df_5m["close"].ewm(span=20, min_periods=3, adjust=False).mean()
            if len(ema20) < 2 or pd.isna(ema20.iloc[-1]) or pd.isna(ema20.iloc[-2]):
                return True, True
            last_close = float(df_5m["close"].iloc[-1])
            is_bullish = float(ema20.iloc[-1]) >= float(ema20.iloc[-2]) and last_close >= float(ema20.iloc[-1])
            is_bearish = float(ema20.iloc[-1]) <= float(ema20.iloc[-2]) and last_close <= float(ema20.iloc[-1])
            return is_bullish, is_bearish
        except Exception:
            return True, True

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        p = self.params
        if ctx.position is not None or df.empty:
            return None

        # ORB struggles heavily in chop. Reject early.
        from app.core.domain import Regime
        if ctx.regime in (Regime.RANGE, Regime.VOLATILE):
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
        if n_today <= rng_n:
            return None

        or_high = float(today_df["high"].iloc[:rng_n].max())
        or_low = float(today_df["low"].iloc[:rng_n].min())
        last = df.iloc[-1]
        close = float(last["close"])
        atr = float(last["atr14"]) if pd.notna(last.get("atr14")) else None
        vol_z = float(last["vol_z"]) if pd.notna(last.get("vol_z")) else 0.0
        rsi14 = float(last["rsi14"]) if pd.notna(last.get("rsi14")) else 50.0
        if atr is None or atr <= 0:
            return None

        if p.get("atr_buffer_mult", 0.0) > 0.0:
            buffer = 1.0 + (p["atr_buffer_mult"] * atr / close)
        else:
            buffer = 1.0 + p["breakout_buffer_bps"] / 10_000.0
        volume_ok = vol_z >= p["volume_z_min"]
        confidence = min(0.85, 0.55 + 0.08 * max(0.0, vol_z))

        # Climax guard: prevent entering if price is stretched more than climax_atr_mult ATR from EMA9
        climax_limit = float(p.get("climax_atr_mult", 2.5))
        ema9 = float(last.get("ema9", close))
        if climax_limit > 0.0 and abs(close - ema9) > climax_limit * atr:
            return None

        # Acceptance gate (sibling-proven): the breakout bar must CLOSE in its
        # directional extreme -- a body-dominant candle shows the range break
        # was accepted, not rejected with a wick.
        min_acceptance = float(p.get("min_acceptance_ratio", 0.50))
        bar_high = float(last["high"]); bar_low = float(last["low"])
        bar_range = max(1e-6, bar_high - bar_low)
        close_pos_in_bar = ((close - bar_low) / bar_range)
        acceptance_long = close_pos_in_bar
        acceptance_short = 1.0 - close_pos_in_bar

        mft_enabled = bool(p.get("mft_alignment", True))
        is_bullish_5m, is_bearish_5m = self._check_5m_trend(df) if mft_enabled else (True, True)

        # Daily drift filter (param-gated; ported from sibling evidence):
        # breakout longs BELOW the 20-day mean and shorts ABOVE it fight
        # persistent order flow -- measured there as the single biggest ORB
        # loss source. Neutral band keeps range days tradable.
        drift_long_ok = drift_short_ok = True
        daily_sma = float(ctx.daily_sma20) if getattr(ctx, "daily_sma20", None) else 0.0
        if p.get("use_daily_drift_filter", True) and daily_sma > 0:
            buf = daily_sma * (float(p.get("drift_buffer_pct", 1.0)) / 100.0)
            drift_long_ok = close > daily_sma - buf
            drift_short_ok = close < daily_sma + buf

        # Minimum-risk geometry floor: skip degenerate entries whose stop would
        # sit unrealistically tight relative to the symbol's own volatility.
        min_risk_atr = float(p.get("min_risk_atr", 0.75))

        if close > or_high * buffer and volume_ok and is_bullish_5m and ctx.regime == Regime.TREND_UP:
            if not drift_long_ok:
                return None
            if acceptance_long < min_acceptance:
                return None
            stop = max(or_low, close - p["atr_stop_mult"] * atr)
            risk = close - stop
            if risk <= 0 or risk < min_risk_atr * atr:
                return None
            self._pending[key] = ctx.now
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=confidence,
                strategy=self.name,
                reason=f"Broke above {rng_n}m range {or_high:.2f} (vol z {vol_z:.1f}, 5m trend aligned)",
                price=close,
                stop_loss=stop,
                take_profit=close + p["reward_risk"] * risk,
            )
        if close < or_low / buffer and volume_ok and is_bearish_5m and ctx.regime == Regime.TREND_DOWN:
            if not drift_short_ok:
                return None
            if acceptance_short < min_acceptance:
                return None
            stop = min(or_high, close + p["atr_stop_mult"] * atr)
            risk = stop - close
            if risk <= 0 or risk < min_risk_atr * atr:
                return None
            self._pending[key] = ctx.now
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=confidence,
                strategy=self.name,
                reason=f"Broke below {rng_n}m range {or_low:.2f} (vol z {vol_z:.1f}, 5m trend aligned)",
                price=close,
                stop_loss=stop,
                take_profit=close - p["reward_risk"] * risk,
            )
        return None
