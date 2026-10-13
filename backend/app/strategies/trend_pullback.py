"""Trend Pullback / EMA Cloud Strategy.

Buys pullbacks in established trends instead of chasing breakouts.
Offers higher reward-to-risk (often 2.5R-3.5R).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

ET = ZoneInfo("America/New_York")


@register
class TrendPullback(Strategy):
    name = "trend_pullback"
    description = "Buys pullbacks into the EMA20/50 value zone during established trends"

    is_breakout = False
    is_mean_reversion = True
    ml_threshold = 0.53
    evaluation_horizons = ["60m", "close"]
    regime_weights = {
        Regime.TREND_UP: 1.20, Regime.TREND_DOWN: 1.20, Regime.RANGE: 0.80, Regime.VOLATILE: 0.80
    }
    # =============================================================================
    # [MODIFIED 2026-10-14]: CR-07 - Add min_risk_atr Parameter
    # ORIGINAL INTENT: default_params had min_er20, max_vol_z, min_wick_ratio, reward_risk.
    # REASON FOR CHANGE: Add min_risk_atr (default 0.5 ATR) to prevent degenerate micro-stop entries.
    # SAFE ROLLBACK: The original code is preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # default_params = {
    #     "min_er20": 0.35,             # Efficiency Ratio (Trend strength)
    #     "max_vol_z": 0.5,             # Max volume z-score on the pullback (low volume dip)
    #     "min_wick_ratio": 0.40,       # Rejection wick must be 40% of candle
    #     "reward_risk": 3.0,
    # }
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    default_params = {
        "min_er20": 0.35,             # Efficiency Ratio (Trend strength)
        "max_vol_z": 0.5,             # Max volume z-score on the pullback (low volume dip)
        "min_wick_ratio": 0.40,       # Rejection wick must be 40% of candle
        "reward_risk": 3.0,
        "min_risk_atr": 0.5,          # CR-07: Minimum EMA-gap risk distance to avoid degenerate setups
    }
    # =============================================================================

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

        p = self.params

        last_row = df.iloc[-1]

        # Required indicators
        for col in ["er20", "ema20", "ema50", "rsi14", "vol_z", "atr14"]:
            if col not in last_row or pd.isna(last_row[col]):
                return None

        er20 = float(last_row["er20"])
        ema20 = float(last_row["ema20"])
        ema50 = float(last_row["ema50"])
        rsi14 = float(last_row["rsi14"])
        vol_z = float(last_row["vol_z"])
        atr14 = float(last_row["atr14"])

        close_px = float(last_row["close"])
        open_px = float(last_row["open"])
        high_px = float(last_row["high"])
        low_px = float(last_row["low"])

        candle_range = max(high_px - low_px, 1e-6)

        # Trend Strength Check
        if er20 < p["min_er20"]:
            return None

        is_uptrend = (ema20 > ema50) and ((ema20 - ema50) / ema50 >= 0.002)
        is_downtrend = (ema20 < ema50) and ((ema50 - ema20) / ema50 >= 0.002)

        # High volume invalidates the "low volume pullback" thesis
        if vol_z > p["max_vol_z"]:
            return None

        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-07 - EMA-Gap Viability Check
        # ORIGINAL INTENT: Entered on any bullish/bearish candle reclaim without checking
        # if the distance between close and (EMA50 +- 0.5*ATR) provided viable economic risk.
        # REASON FOR CHANGE: When close is right near the stop line, the stop distance is < 0.5 ATR,
        # leading to degenerate setups chopped by noise and spread. Require risk distance >= 0.5*ATR.
        # SAFE ROLLBACK: The original code is preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # # Long Setup (Uptrend Pullback)
        # reward_risk = float(p["reward_risk"])
        # if is_uptrend:
        #     # 1. Price dipped into the Value Zone (between EMA20 and EMA50 - small buffer)
        #     if low_px <= ema20 and low_px >= (ema50 - 0.5 * atr14):
        #         # 2. RSI is cooled down (not overbought, not oversold)
        #         if 40.0 <= rsi14 <= 55.0:
        #             # 3. Trigger: Bullish rejection candle closing back above EMA20
        #             lower_wick = min(open_px, close_px) - low_px
        #             wick_ratio = lower_wick / candle_range
        #
        #             if close_px > open_px and close_px > ema20 and wick_ratio >= p["min_wick_ratio"]:
        #                 confidence = min(0.90, 0.55 + (0.15 * min(2.0, er20)) + (0.10 * min(1.0, wick_ratio)))
        #                 return Signal(
        #                     symbol=symbol,
        #                     action=SignalAction.ENTER_LONG,
        #                     confidence=round(confidence, 4),
        #                     strategy=self.name,
        #                     reason=f"Trend pullback (er20 {er20:.2f}, wick {wick_ratio:.2f})",
        #                     price=close_px,
        #                     score=0.80, # Very high probability setup
        #                     stop_loss=ema50 - 0.5 * atr14,
        #                     take_profit=close_px + (close_px - (ema50 - 0.5 * atr14)) * reward_risk,
        #                 )
        #
        # # Short Setup (Downtrend Pullback)
        # elif is_downtrend:
        #     # 1. Price popped into the Value Zone
        #     if high_px >= ema20 and high_px <= (ema50 + 0.5 * atr14):
        #         # 2. RSI is cooled down
        #         if 45.0 <= rsi14 <= 60.0:
        #             # 3. Trigger: Bearish rejection candle closing back below EMA20
        #             upper_wick = high_px - max(open_px, close_px)
        #             wick_ratio = upper_wick / candle_range
        #
        #             if close_px < open_px and close_px < ema20 and wick_ratio >= p["min_wick_ratio"]:
        #                 confidence = min(0.90, 0.55 + (0.15 * min(2.0, er20)) + (0.10 * min(1.0, wick_ratio)))
        #                 return Signal(
        #                     symbol=symbol,
        #                     action=SignalAction.ENTER_SHORT,
        #                     confidence=round(confidence, 4),
        #                     strategy=self.name,
        #                     reason=f"Trend pullback short (er20 {er20:.2f}, wick {wick_ratio:.2f})",
        #                     price=close_px,
        #                     score=0.80,  # Very high probability setup
        #                     stop_loss=ema50 + 0.5 * atr14,
        #                     take_profit=close_px - ((ema50 + 0.5 * atr14) - close_px) * reward_risk,
        #                 )
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        min_risk_dist = float(p.get("min_risk_atr", 0.5)) * atr14
        reward_risk = float(p["reward_risk"])
        if is_uptrend:
            # 1. Price dipped into the Value Zone (between EMA20 and EMA50 - small buffer)
            if low_px <= ema20 and low_px >= (ema50 - 0.5 * atr14):
                # 2. RSI is cooled down (not overbought, not oversold)
                if 40.0 <= rsi14 <= 55.0:
                    # 3. Trigger: Bullish rejection candle closing back above EMA20
                    lower_wick = min(open_px, close_px) - low_px
                    wick_ratio = lower_wick / candle_range

                    if close_px > open_px and close_px > ema20 and wick_ratio >= p["min_wick_ratio"]:
                        stop_loss = ema50 - 0.5 * atr14
                        risk_distance = close_px - stop_loss
                        if risk_distance < min_risk_dist:
                            return None
                        confidence = min(0.90, 0.55 + (0.15 * min(2.0, er20)) + (0.10 * min(1.0, wick_ratio)))
                        return Signal(
                            symbol=symbol,
                            action=SignalAction.ENTER_LONG,
                            confidence=round(confidence, 4),
                            strategy=self.name,
                            reason=f"Trend pullback (er20 {er20:.2f}, wick {wick_ratio:.2f})",
                            price=close_px,
                            score=0.80,  # Very high probability setup
                            stop_loss=stop_loss,
                            take_profit=close_px + risk_distance * reward_risk,
                        )

        # Short Setup (Downtrend Pullback)
        elif is_downtrend:
            # 1. Price popped into the Value Zone
            if high_px >= ema20 and high_px <= (ema50 + 0.5 * atr14):
                # 2. RSI is cooled down
                if 45.0 <= rsi14 <= 60.0:
                    # 3. Trigger: Bearish rejection candle closing back below EMA20
                    upper_wick = high_px - max(open_px, close_px)
                    wick_ratio = upper_wick / candle_range

                    if close_px < open_px and close_px < ema20 and wick_ratio >= p["min_wick_ratio"]:
                        stop_loss = ema50 + 0.5 * atr14
                        risk_distance = stop_loss - close_px
                        if risk_distance < min_risk_dist:
                            return None
                        confidence = min(0.90, 0.55 + (0.15 * min(2.0, er20)) + (0.10 * min(1.0, wick_ratio)))
                        return Signal(
                            symbol=symbol,
                            action=SignalAction.ENTER_SHORT,
                            confidence=round(confidence, 4),
                            strategy=self.name,
                            reason=f"Trend pullback short (er20 {er20:.2f}, wick {wick_ratio:.2f})",
                            price=close_px,
                            score=0.80,  # Very high probability setup
                            stop_loss=stop_loss,
                            take_profit=close_px - risk_distance * reward_risk,
                        )
        # =============================================================================

        return None
