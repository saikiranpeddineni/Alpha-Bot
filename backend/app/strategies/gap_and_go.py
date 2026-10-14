"""Gap & Go Strategy.

Exploits pre-market gap stocks with strong volume.
Leverages the scanner's mover context.
Uses VWAP + EMA confirmation and requires a bullish/bearish candle close.
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class GapAndGo(Strategy):
    name = "gap_and_go"
    description = "Pre-market catalyst continuation (Gap & Go)"

    is_breakout = True
    # Gap-and-go follows an already extended mover.  Treating it as a
    # structural breakout would exempt the exact late-continuation failure
    # class the session-extreme guard was added to prevent.
    is_structural_breakout = False
    ml_threshold = 0.54
    evaluation_horizons = ["60m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.15, Regime.TREND_DOWN: 1.15, Regime.RANGE: 0.70, Regime.VOLATILE: 0.90
    }
    default_params = {
        "min_gap_pct": 2.0,           # Minimum gap percentage
        "vol_z_min": 1.5,             # Minimum volume Z-score
        "start_time": "09:35",        # Only trade after the first 5 minutes established
        "end_time": "11:30",          # Stop trading after morning drive
        "reward_risk": 2.5,
        # =============================================================================
        # [ADDED 2026-10-14]: CR-05 — Gap&Go max_gap_pct and exhaustion guard
        # ORIGINAL INTENT: No upper gap bound; all gaps >= min_gap_pct permitted.
        # REASON FOR CHANGE: Aug-24 data shows 3/3 shorts into >4% grind-down lows all losers.
        #   Gaps >8% are already spent — the move happened pre-market; momentum entry chases.
        #   Exhaustion guard catches the multi-bar downtrend continuation case: don't short
        #   what's already in free-fall into session lows (need to wait for re-arm / bounce).
        # SAFE ROLLBACK: Set max_gap_pct=100.0, exhaustion_atr=0.0, exhaustion_pct=100.0
        "max_gap_pct": 8.0,           # CR-05: gaps >8% are already spent (skip)
        "exhaustion_atr": 2.5,        # CR-05: skip short within 2.5 ATR of session low
        "exhaustion_pct": 3.0,        # CR-05: skip short when 5-bar return < -3% already down
        # =============================================================================
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
        if ctx.position is not None or df.empty:
            return None

        et_dates = df.index.tz_convert(ET).date
        et_times = df.index.tz_convert(ET).time
        today = et_dates[-1]
        now_time = et_times[-1]

        # Only trade once per symbol per day
        if (symbol, today) in self._traded:
            return None

        # Time gating (Morning Drive only)
        p = self.params
        start_t = time.fromisoformat(p["start_time"])
        end_t = time.fromisoformat(p["end_time"])
        if not (start_t <= now_time <= end_t):
            return None

        # Requires scanner gap context
        gap_pct = ctx.mover_change_pct
        min_gap = p["min_gap_pct"]
        is_gap_up = gap_pct >= min_gap
        is_gap_down = gap_pct <= -min_gap

        if not (is_gap_up or is_gap_down):
            return None

        # =============================================================================
        # [ADDED 2026-10-14]: CR-05 — max_gap_pct (spent gap filter)
        # ORIGINAL INTENT: No upper bound on gap size; all qualifying gaps emitted.
        # REASON FOR CHANGE: Gaps >8% have already made their move pre-market. Entering
        #   a market-order continuation into a >8% gap chases an exhausted move.
        #   These should route to pullback re-arm (CR-02b) rather than direct entry.
        # SAFE ROLLBACK: Set max_gap_pct=100.0
        max_gap = float(p.get("max_gap_pct", 8.0))
        if abs(gap_pct) > max_gap:
            return None
        # =============================================================================

        last_row = df.iloc[-1]
        close_px = float(last_row["close"])
        open_px = float(last_row["open"])
        vwap = float(last_row["vwap"])
        ema9 = float(last_row["ema9"])
        ema20 = float(last_row["ema20"])
        vol_z = float(last_row["vol_z"])

        # Require high relative volume
        if vol_z < p["vol_z_min"]:
            return None

        # Calculate confidence based on volume and trend alignment
        ema_spread_pct = abs(ema9 - ema20) / ema20 * 100.0
        confidence = min(0.90, 0.55 + (0.05 * max(0.0, vol_z)) + (0.05 * min(2.0, ema_spread_pct)))

        # Long Setup (Gap Up)
        reward_risk = float(p["reward_risk"])
        if is_gap_up:
            # close > ema20 guarantees stop (ema20-0.05) sits below entry and the
            # R-multiple target sits above entry.
            if close_px > vwap and ema9 > ema20 and close_px > ema20 and close_px > open_px:
                # =============================================================================
                # [ADDED 2026-10-14]: CR-05 — Long exhaustion guard (symmetric to short)
                # ORIGINAL INTENT: No exhaustion check for longs; all VWAP/EMA-aligned gaps emitted.
                # REASON FOR CHANGE: Long gap-up into session high after already-extended 5-bar run
                #   is chasing. Skip when close within 2.5 ATR of session high AND 5-bar return > 3%.
                # SAFE ROLLBACK: Remove the exhaustion guard block.
                atr_px = float(last_row.get("atr14", 0.0))
                exhaustion_atr = float(p.get("exhaustion_atr", 2.5))
                exhaustion_pct = float(p.get("exhaustion_pct", 3.0))
                if atr_px > 0:
                    session_df = df[df.index.tz_convert(ET).date == today]
                    session_high = float(session_df["high"].max()) if len(session_df) > 0 else close_px
                    within_session_high = (session_high - close_px) < exhaustion_atr * atr_px
                    five_bar_return_pct = 0.0
                    if len(df) >= 5:
                        five_bar_return_pct = (float(df["close"].iloc[-1]) - float(df["close"].iloc[-5])) / max(1e-6, abs(float(df["close"].iloc[-5]))) * 100.0
                    is_exhausted_long = within_session_high and five_bar_return_pct >= exhaustion_pct
                    if is_exhausted_long:
                        return None  # Already extended into session high; pullback re-arm instead
                # =============================================================================
                # Confirmed gap & go long
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_LONG,
                    confidence=round(confidence, 4),
                    strategy=self.name,
                    reason=f"Gap up continuation breakout (vol z {vol_z:.1f})",
                    price=close_px,
                    score=0.75,
                    stop_loss=ema20 - 0.05,
                    take_profit=close_px + (close_px - ema20) * reward_risk,
                )

        # Short Setup (Gap Down)
        elif is_gap_down:
            # close < ema20 guarantees stop (ema20+0.05) sits above entry and the
            # R-multiple target sits below entry.
            if close_px < vwap and ema9 < ema20 and close_px < ema20 and close_px < open_px:
                # =============================================================================
                # [ADDED 2026-10-14]: CR-05 — Short exhaustion guard
                # ORIGINAL INTENT: No exhaustion check; all VWAP/EMA-aligned shorts emitted.
                # REASON FOR CHANGE: Aug-24 evidence: 3/3 gap-down shorts into >4% grind-down
                #   session lows were ALL losers. Shorting into exhaustion is chasing the move.
                #   Skip when close within 2.5 ATR of session low AND 5-bar return < -3%.
                # SAFE ROLLBACK: Remove the exhaustion guard block.
                atr_px = float(last_row.get("atr14", 0.0))
                exhaustion_atr = float(p.get("exhaustion_atr", 2.5))
                exhaustion_pct = float(p.get("exhaustion_pct", 3.0))
                if atr_px > 0:
                    session_df = df[df.index.tz_convert(ET).date == today]
                    session_low = float(session_df["low"].min()) if len(session_df) > 0 else close_px
                    within_session_low = (close_px - session_low) < exhaustion_atr * atr_px
                    five_bar_return_pct = 0.0
                    if len(df) >= 5:
                        five_bar_return_pct = (float(df["close"].iloc[-1]) - float(df["close"].iloc[-5])) / max(1e-6, abs(float(df["close"].iloc[-5]))) * 100.0
                    is_exhausted_short = within_session_low and five_bar_return_pct <= -exhaustion_pct
                    if is_exhausted_short:
                        return None  # Already in freefall into session low; pullback re-arm instead
                # =============================================================================
                # Confirmed gap & go short
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_SHORT,
                    confidence=round(confidence, 4),
                    strategy=self.name,
                    reason=f"Gap down continuation breakdown (vol z {vol_z:.1f})",
                    price=close_px,
                    score=0.75,
                    stop_loss=ema20 + 0.05,
                    take_profit=close_px - (ema20 - close_px) * reward_risk,
                )
