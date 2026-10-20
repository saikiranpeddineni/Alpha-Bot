"""Relative Strength Strategy.

Cross-Asset Momentum Alpha.
Triggers when a symbol has extreme relative strength compared to the SPY,
heavy accumulation volume, and makes local highs.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class RelativeStrength(Strategy):
    name = "relative_strength"
    description = "Trades institutional leaders and laggards relative to SPY"

    is_breakout = False
    ml_threshold = 0.54
    evaluation_horizons = ["close"]
    regime_weights = {
        Regime.TREND_UP: 1.20, Regime.TREND_DOWN: 0.80, Regime.RANGE: 1.00, Regime.VOLATILE: 0.90
    }
    default_params = {
        "rs_long_threshold": 0.70,    # 0.70 to 1.0 means strong outperformance vs SPY (was 0.75;
                                      # 0.75 coincided with local-high breakout so rarely that the
                                      # trainer found zero lifetime candidate events)
        "rs_short_threshold": 0.30,   # 0.0 to 0.30 means strong underperformance vs SPY (was 0.25)
        "vol_z_min": 1.0,             # Require volume conviction (was 1.2)
        "reward_risk": 2.5,
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
        if ctx.position is not None or len(df) < 30:
            return None

        # Requires RS score from the engine
        if ctx.rs_score is None:
            return None

        et_dates = df.index.tz_convert(ET).date
        et_times = df.index.tz_convert(ET).time
        today = et_dates[-1]
        now_time = et_times[-1]

        if (symbol, today) in self._traded:
            return None

        p = self.params

        last_row = df.iloc[-1]
        if "vol_z" not in last_row or pd.isna(last_row["vol_z"]):
            return None

        vol_z = float(last_row["vol_z"])
        if vol_z < p["vol_z_min"]:
            return None

        close_px = float(last_row["close"])

        # Calculate local 30-minute high/low
        local_df = df.iloc[-30:-1]
        if local_df.empty:
            return None

        local_high = float(local_df["high"].max())
        local_low = float(local_df["low"].min())

        # =============================================================================
        # [MODIFIED 2026-10-14]: L21 & L22 - Native Stop/Target Geometry & Breakout Buffer
        # ORIGINAL INTENT: Emitted Signal without stop_loss or take_profit; checked close_px > local_high
        # without any ATR buffer, letting $0.01 nominal tag highs pass into generic excursion fills.
        # REASON FOR CHANGE: Supply native ATR-based stop and target geometry (2.0 ATR stop, 2.5R target)
        # and require a 0.1 ATR breakout buffer to prevent spread noise triggers.
        # SAFE ROLLBACK: The original code is removed; rollback via git history.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        atr14 = float(last_row["atr14"]) if ("atr14" in last_row and not pd.isna(last_row["atr14"])) else (close_px * 0.015)
        stop_dist = max(atr14 * 2.0, close_px * 0.005)
        reward_risk = float(p.get("reward_risk", 2.5))
        buffer = 0.1 * atr14

        # Long Setup (High Relative Strength)
        if ctx.rs_score >= p["rs_long_threshold"]:
            if close_px >= (local_high + buffer):
                confidence = min(0.90, 0.60 + 0.1 * (ctx.rs_score - p["rs_long_threshold"]))
                stop_loss = round(close_px - stop_dist, 4)
                take_profit = round(close_px + stop_dist * reward_risk, 4)
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_LONG,
                    confidence=round(confidence, 4),
                    strategy=self.name,
                    reason=f"RS score {ctx.rs_score:.2f} > {p['rs_long_threshold']} and broke local high",
                    score=0.75,
                    price=close_px,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )

        # Short Setup (High Relative Weakness)
        if ctx.rs_score <= p["rs_short_threshold"]:
            if close_px <= (local_low - buffer):
                confidence = min(0.90, 0.60 + 0.1 * (p["rs_short_threshold"] - ctx.rs_score))
                stop_loss = round(close_px + stop_dist, 4)
                take_profit = round(close_px - stop_dist * reward_risk, 4)
                return Signal(
                    symbol=symbol,
                    action=SignalAction.ENTER_SHORT,
                    confidence=round(confidence, 4),
                    strategy=self.name,
                    reason=f"RS score {ctx.rs_score:.2f} < {p['rs_short_threshold']} and broke local low",
                    score=0.75,
                    price=close_px,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
        # =============================================================================

        return None
