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
    # =============================================================================
    # [MODIFIED 2026-10-22]: Align ORB with Betadesk Proven Configuration & Regime Weights
    # ORIGINAL INTENT: regime_weights RANGE was 0.70 / VOLATILE 1.00, and default_params
    # had start_time 10:00, end_time 14:00, volume_z_min 1.5 (shorts 2.0), min_risk_atr 0.75,
    # min_range_atr 0.5, and climax_atr_mult 2.5.
    # REASON FOR CHANGE: Forensic audit shows these restrictive theoretical filters choked off
    # all prime morning breakout winners between 09:45-10:00 ET and in non-trend regimes.
    # Aligning with Betadesk parameters unlocks genuine expansion runners while introducing
    # smart VWAP alignment and a 0.45% institutional stop cushion.
    # SAFE ROLLBACK: Previous regime_weights and default_params preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # regime_weights = {
    #     Regime.TREND_UP: 1.15, Regime.TREND_DOWN: 1.15, Regime.RANGE: 0.70, Regime.VOLATILE: 1.00
    # }
    # default_params = {
    #     "range_minutes": 15,
    #     "breakout_buffer_bps": 5,     # close must clear the range by 0.05%
    #     "atr_buffer_mult": 0.0,       # if > 0, overrides breakout_buffer_bps with (mult * ATR)
    #     "volume_z_min": 1.5,
    #     "short_volume_z_min": 2.0,     # REWRITE 2026-10-16: shorts need vol_z >= 2.0
    #                                     # (rollback = 1.5, env ORB_SHORT_VOL_Z)
    #     "short_acceptance": 0.60,       # REWRITE 2026-10-16: shorts always need 0.60
    #                                     # acceptance (rollback = 0.50, env ORB_SHORT_ACCEPTANCE)
    #     "atr_stop_mult": 2.0,         # risk cap when the range is very wide
    #     "reward_risk": 2.0,
    #     "trailing_stop_atr_mult": 1.5, # if > 0, activates dynamic trailing stop
    #     "mft_alignment": False,       # optional 5m higher-timeframe trend alignment (set True in config)
    #     "use_daily_drift_filter": True,  # fade-proofing: don't fight the 20-day trend
    #     "drift_buffer_pct": 1.0,         # +/- % around daily SMA20 that counts as neutral
    #     "min_risk_atr": 0.75,            # skip entries whose stop sits unrealistically tight
    #     "min_acceptance_ratio": 0.50,    # breakout bar must CLOSE in its directional
    #                                      # extreme (body-dominant = accepted breakout,
    #                                      # not a wick fake-out). Sibling-proven gate.
    #     "climax_atr_mult": 2.5,          # relaxed from 1.5: breakouts legitimately detach
    #                                      # from EMA9; acceptance+drift now carry quality.
    #     "end_time": "14:00",             # REWRITE 2026-10-16: no new ORB after 14:00 ET (was 15:00).
    #     "start_time": "10:00",           # REWRITE 2026-10-16: no new ORB before 10:00 ET.
    #     "acceptance_lowvol": 0.60,       # CR-03: tighter acceptance when vol_z < 2.0 (wick fakeout guard)
    #     "min_range_atr": 0.5,            # CR-03: skip degenerate ranges smaller than 0.5 ATR
    # }
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    regime_weights = {
        Regime.TREND_UP: 1.20,
        Regime.TREND_DOWN: 1.20,
        Regime.RANGE: 0.55,    # Betadesk calibrated: permits genuine expansion breakouts in range
        Regime.VOLATILE: 0.50,
    }
    default_params = {
        "range_minutes": 15,
        "breakout_buffer_bps": 5,        # close must clear the range by 0.05%
        "atr_buffer_mult": 0.0,          # if > 0, overrides breakout_buffer_bps with (mult * ATR)
        "volume_z_min": 0.0,             # Betadesk baseline: allows steady institutional trends
        "short_volume_z_min": 0.0,       # Betadesk baseline: 0.0
        "short_acceptance": 0.50,        # standard directional acceptance
        "atr_stop_mult": 1.5,            # Betadesk calibrated stop multiplier (was 2.0)
        "reward_risk": 2.5,              # Betadesk calibrated take profit target (was 2.0)
        "chop_reward_risk": 0.75,        # Betadesk 2-tier take-profit: 0.75R in chop regimes
        "trailing_stop_atr_mult": 1.5,   # activates dynamic trailing stop
        "mft_alignment": False,          # optional 5m higher-timeframe trend alignment
        "use_daily_drift_filter": True,  # fade-proofing: don't fight the 20-day trend
        "drift_buffer_pct": 1.0,         # +/- % around daily SMA20
        "min_risk_atr": 0.0,             # Betadesk baseline: 0.0 (allows tight setups)
        "min_acceptance_ratio": 0.50,    # breakout bar must CLOSE in its directional extreme
        "climax_atr_mult": 10.0,         # Betadesk baseline: 10.0 (blocks blowoffs >10 ATR, lets runners run)
        "end_time": "14:45",             # Betadesk baseline: 14:45 ET cutoff (allows 14:00-14:35 runners)
        "start_time": "09:45",           # Betadesk baseline: 09:45 ET (immediately after 15m range is set)
        "acceptance_lowvol": 0.50,       # standard acceptance
        "min_range_atr": 0.0,            # Betadesk baseline: 0.0 (allows coiling range breakouts)
        "min_stop_cushion_pct": 0.0045,  # 0.45% minimum stop cushion prevents noise shakeouts
        "require_vwap_alignment": True,  # Longs >= VWAP, Shorts <= VWAP
        "avoid_counter_trend_regime": True, # Reject Long in TREND_DOWN, Short in TREND_UP
        "vwap_fade_bars": 2,             # 2 consecutive closes beyond VWAP cuts failed breakouts
    }
    # =============================================================================

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        super().__init__(params, enabled)
        self._traded: set[tuple[str, object]] = set()
        self._pending: dict[tuple[str, object], datetime] = {}
        self._vwap_fade_count: dict[str, int] = {}

    def reset(self) -> None:
        self._traded.clear()
        self._pending.clear()
        self._vwap_fade_count.clear()

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
        if df.empty:
            return None

        last = df.iloc[-1]
        close = float(last["close"])
        et_dates = df.index.tz_convert(ET).date
        et_times = df.index.tz_convert(ET).time
        today = et_dates[-1]
        now_time = et_times[-1]

        # =============================================================================
        # [MODIFIED 2026-10-22]: In-Trade Position Management (Betadesk Thesis Exits)
        # ORIGINAL INTENT: ctx.position is not None returned None unconditionally, ignoring
        # active positions and leaving exit logic solely to generic stops.
        # REASON FOR CHANGE: Port Betadesk's proven position management:
        #   1. EOD Exit at 15:55 ET.
        #   2. 2-Bar VWAP Invalidation Exit for non-runners (cuts losing breakouts at -0.3R/-0.5R).
        #   3. Protects runners (>= 1.2 ATR profit) from being shaken out by minor VWAP dips.
        # SAFE ROLLBACK: Previous early return preserved below.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if ctx.position is not None or df.empty:
        #     return None
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if ctx.position is not None:
            if ctx.position.strategy != self.name:
                return None
            from datetime import time as dtime
            # 1. EOD Exit (15:55 ET)
            if now_time >= dtime(15, 55):
                return Signal(
                    symbol=symbol,
                    action=SignalAction.EXIT,
                    confidence=1.0,
                    strategy=self.name,
                    reason="End of day exit (15:55 ET)",
                    price=close,
                )
            # 2. VWAP Invalidation Exit for non-runners
            atr_val = float(last.get("atr14", close * 0.01)) if pd.notna(last.get("atr14")) else (close * 0.01)
            profit_dist = (close - ctx.position.avg_entry_price) if ctx.position.qty > 0 else (ctx.position.avg_entry_price - close)
            is_runner = profit_dist >= (1.20 * atr_val)
            if not is_runner:
                vwap_val = float(last.get("vwap", close)) if pd.notna(last.get("vwap")) else None
                if vwap_val is not None and vwap_val > 0:
                    if ctx.position.qty > 0 and close < vwap_val:
                        self._vwap_fade_count[symbol] = self._vwap_fade_count.get(symbol, 0) + 1
                    elif ctx.position.qty < 0 and close > vwap_val:
                        self._vwap_fade_count[symbol] = self._vwap_fade_count.get(symbol, 0) + 1
                    else:
                        self._vwap_fade_count[symbol] = 0

                    fade_bars = int(p.get("vwap_fade_bars", 2))
                    if self._vwap_fade_count.get(symbol, 0) >= fade_bars:
                        reason = "Momentum faded (Close < VWAP)" if ctx.position.qty > 0 else "Momentum faded (Close > VWAP)"
                        return Signal(
                            symbol=symbol,
                            action=SignalAction.EXIT,
                            confidence=1.0,
                            strategy=self.name,
                            reason=reason,
                            price=close,
                        )
            return None
        # =============================================================================

        # =============================================================================
        # [MODIFIED 2026-10-22]: Remove Hard Chop Disqualification (Port Betadesk Adaptive Handling)
        # ORIGINAL INTENT: Unconditionally returned None when ctx.regime in (RANGE, VOLATILE).
        # REASON FOR CHANGE: Blocked prime morning breakouts since opening 15 bars often register
        # as RANGE. Betadesk weights RANGE at 0.55 and pairs it with 0.75R chop defense.
        # SAFE ROLLBACK: Previous lines preserved below.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # from app.core.domain import Regime
        # if ctx.regime in (Regime.RANGE, Regime.VOLATILE):
        #     return None
        # -----------------------------------------------------------------------------
        # =============================================================================

        et_dates = df.index.tz_convert(ET).date
        et_times = df.index.tz_convert(ET).time
        today = et_dates[-1]
        now_time = et_times[-1]

        # =============================================================================
        # [ADDED 2026-10-14]: CR-20 — NVDA stale-bar anomaly guard
        # ORIGINAL INTENT: today was taken directly from df.index.tz_convert(ET).date[-1].
        # REASON FOR CHANGE: If df was stale, today evaluated to yesterday's date, causing ORB
        #   to evaluate yesterday's opening range as today's (e.g. NVDA px=128.92 incident).
        #   If ctx.now is provided, require that the latest bar's date matches ctx.now's session date.
        # [NEW CODE]:
        if ctx.now is not None:
            ctx_date = ctx.now.astimezone(ET).date()
            if today != ctx_date:
                return None
        # =============================================================================

        key = (symbol, today)
        if key in self._traded:
            return None

        # =============================================================================
        # [ADDED 2026-10-14]: CR-03 — ORB end_time cutoff (no new ORB after 15:00 ET)
        # ORIGINAL INTENT: No time upper bound — ORB entries allowed until session close.
        # REASON FOR CHANGE: Late-day ORB entries (post-15:00) with insufficient PIS time
        #   ride to EOD flatten for full -1R. ORB edge is strictly a morning phenomenon.
        #   Existing runners after 15:00 are still managed by PIS (this only blocks new entries).
        import os
        # =============================================================================
        # [MODIFIED 2026-10-22]: Align ORB Entry Windows, Stops, and Adaptive Targets with Betadesk
        # ORIGINAL INTENT: start_time='10:00', end_time='14:00', hard check ctx.regime == TREND_UP/DOWN,
        # min_risk_atr=0.75, static reward_risk=2.0.
        # REASON FOR CHANGE: Unblock prime 09:45-10:00 ET expansion window, allow breakouts in
        # RANGE with 0.75R chop take-profit defense, add smart VWAP alignment, enforce 0.45%
        # stop cushion, and expand trend runner target to 2.5R.
        # SAFE ROLLBACK: Previous entry logic preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # import os
        # end_time_str = os.environ.get("ORB_END_TIME", p.get("end_time", "14:00"))
        # start_time_str = os.environ.get("ORB_START_TIME", p.get("start_time", "10:00"))
        # from datetime import time as dtime
        # end_t = dtime.fromisoformat(end_time_str)
        # start_t = dtime.fromisoformat(start_time_str)
        # if now_time > end_t:
        #     return None
        # if now_time < start_t:
        #     return None
        # ...
        # if close > or_high * buffer and volume_ok and is_bullish_5m and ctx.regime == Regime.TREND_UP:
        #     ...
        # if close < or_low / buffer and volume_ok_short and is_bearish_5m and ctx.regime == Regime.TREND_DOWN:
        #     ...
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        import os
        end_time_str = os.environ.get("ORB_END_TIME", p.get("end_time", "14:45"))
        start_time_str = os.environ.get("ORB_START_TIME", p.get("start_time", "09:45"))
        from datetime import time as dtime
        end_t = dtime.fromisoformat(end_time_str)
        start_t = dtime.fromisoformat(start_time_str)
        if now_time > end_t:
            return None
        if now_time < start_t:
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

        min_range_atr_mult = float(p.get("min_range_atr", 0.0))
        if min_range_atr_mult > 0.0 and (or_high - or_low) < min_range_atr_mult * atr:
            return None

        if p.get("atr_buffer_mult", 0.0) > 0.0:
            buffer = 1.0 + (p["atr_buffer_mult"] * atr / close)
        else:
            buffer = 1.0 + p["breakout_buffer_bps"] / 10_000.0

        min_vol = float(p.get("volume_z_min", 0.0))
        volume_ok = (vol_z >= min_vol) if min_vol > 0 else True

        import os as _os
        short_vol_min = float(_os.environ.get(
            "ORB_SHORT_VOL_Z", p.get("short_volume_z_min", 0.0)))
        volume_ok_short = (vol_z >= max(min_vol, short_vol_min)) if (min_vol > 0 or short_vol_min > 0) else True
        confidence = min(0.85, 0.55 + 0.08 * max(0.0, vol_z))

        # Climax guard: prevent entering if price is stretched more than climax_atr_mult ATR from EMA9
        climax_limit = float(p.get("climax_atr_mult", 10.0))
        ema9 = float(last.get("ema9", close))
        if climax_limit > 0.0 and abs(close - ema9) > climax_limit * atr:
            return None

        min_acceptance_base = float(p.get("min_acceptance_ratio", 0.50))
        min_acceptance_lowvol = float(p.get("acceptance_lowvol", 0.50))
        min_acceptance = min_acceptance_lowvol if vol_z < 2.0 else min_acceptance_base

        bar_high = float(last["high"]); bar_low = float(last["low"])
        bar_range = max(1e-6, bar_high - bar_low)
        close_pos_in_bar = ((close - bar_low) / bar_range)
        acceptance_long = close_pos_in_bar
        acceptance_short = 1.0 - close_pos_in_bar

        mft_enabled = bool(p.get("mft_alignment", False))
        is_bullish_5m, is_bearish_5m = self._check_5m_trend(df) if mft_enabled else (True, True)

        # Daily drift filter: don't buy far below 20-day SMA or short far above it
        drift_long_ok = drift_short_ok = True
        daily_sma = float(ctx.daily_sma20) if getattr(ctx, "daily_sma20", None) else 0.0
        if p.get("use_daily_drift_filter", True) and daily_sma > 0:
            buf = daily_sma * (float(p.get("drift_buffer_pct", 1.0)) / 100.0)
            drift_long_ok = close > daily_sma - buf
            drift_short_ok = close < daily_sma + buf

        min_risk_atr = float(p.get("min_risk_atr", 0.0))
        vwap_val = float(last.get("vwap", 0.0)) if pd.notna(last.get("vwap")) else None
        require_vwap = bool(p.get("require_vwap_alignment", True))
        avoid_counter = bool(p.get("avoid_counter_trend_regime", True))

        # Check Long Breakout
        if close > or_high * buffer and volume_ok and is_bullish_5m:
            if require_vwap and vwap_val is not None and close < vwap_val:
                return None
            if avoid_counter:
                if ctx.regime == Regime.TREND_DOWN:
                    return None
                if getattr(ctx, "spy_regime", None) == Regime.TREND_DOWN and getattr(ctx, "spy_vwap_bias", None) == "below":
                    return None
            if not drift_long_ok:
                return None
            if acceptance_long < min_acceptance:
                return None
            structural_stop = or_low
            safety_stop = close - float(p.get("atr_stop_mult", 1.5)) * atr
            stop = max(structural_stop, safety_stop)
            min_stop_cushion = close * float(p.get("min_stop_cushion_pct", 0.0045))
            stop = min(stop, close - min_stop_cushion)
            risk = close - stop
            if risk <= 0:
                return None
            if min_risk_atr > 0 and risk < min_risk_atr * atr:
                return None
            self._pending[key] = ctx.now
            is_chop = (ctx.regime in (Regime.RANGE, Regime.VOLATILE))
            rr_mult = float(p.get("chop_reward_risk", 0.75)) if is_chop else float(p.get("reward_risk", 2.5))
            target = close + rr_mult * risk
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                confidence=confidence,
                strategy=self.name,
                reason=f"Broke above {rng_n}m range {or_high:.2f} (vol z {vol_z:.1f})",
                price=close,
                stop_loss=round(stop, 4),
                take_profit=round(target, 4),
            )

        # Check Short Breakout
        leveraged_bull_etfs = {"TQQQ", "UPRO", "SOXL", "SPXL", "FAS", "TECL", "NVDL", "LABU", "BULZ"}
        if close < or_low / buffer and volume_ok_short and is_bearish_5m:
            if symbol.upper() in leveraged_bull_etfs:
                return None
            if require_vwap and vwap_val is not None and close > vwap_val:
                return None
            if avoid_counter:
                if ctx.regime == Regime.TREND_UP:
                    return None
                if getattr(ctx, "spy_vwap_bias", None) == "above" or getattr(ctx, "spy_regime", None) == Regime.TREND_UP:
                    return None
            if not drift_short_ok:
                return None
            if acceptance_short < min_acceptance:
                return None
            structural_stop = or_high
            safety_stop = close + float(p.get("atr_stop_mult", 1.5)) * atr
            stop = min(structural_stop, safety_stop)
            min_stop_cushion = close * float(p.get("min_stop_cushion_pct", 0.0045))
            stop = max(stop, close + min_stop_cushion)
            risk = stop - close
            if risk <= 0:
                return None
            if min_risk_atr > 0 and risk < min_risk_atr * atr:
                return None
            self._pending[key] = ctx.now
            is_chop = (ctx.regime in (Regime.RANGE, Regime.VOLATILE))
            rr_mult = float(p.get("chop_reward_risk", 0.75)) if is_chop else float(p.get("reward_risk", 2.5))
            target = close - rr_mult * risk
            return Signal(
                symbol=symbol,
                action=SignalAction.ENTER_SHORT,
                confidence=confidence,
                strategy=self.name,
                reason=f"Broke below {rng_n}m range {or_low:.2f} (vol z {vol_z:.1f})",
                price=close,
                stop_loss=round(stop, 4),
                take_profit=round(target, 4),
            )

        return None
        # =============================================================================
