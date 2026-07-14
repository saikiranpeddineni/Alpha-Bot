"""Session Clock Phase and Market Behavior State classifiers.
"""

from __future__ import annotations

from enum import StrEnum
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

from app.indicators.ta import efficiency_ratio, atr, session_vwap, ema

ET = ZoneInfo("America/New_York")


class SessionClockPhase(StrEnum):
    PREMARKET = "premarket"
    AUCTION_CONTEXT = "auction_context"
    OPEN_0_5 = "open_0_5"
    OPEN_5_30 = "open_5_30"
    MORNING = "morning"
    MIDDAY = "midday"
    AFTERNOON = "afternoon"
    POWER_HOUR = "power_hour"
    CLOSE_WINDOW = "close_window"


class MarketBehaviorState(StrEnum):
    PRICE_DISCOVERY = "price_discovery"
    TREND_EXPANSION = "trend_expansion"
    TREND_CONTINUATION = "trend_continuation"
    COMPRESSION = "compression"
    RANGE = "range"
    CHOP = "chop"
    REVERSAL = "reversal"
    VOLATILITY_SHOCK = "volatility_shock"


def classify_clock_phase(ts: datetime) -> SessionClockPhase:
    """Classify the clock phase of the session based on ET time."""
    et_dt = ts.astimezone(ET)
    minute_of_day = et_dt.hour * 60 + et_dt.minute

    # Market hours are 9:30 AM (570 mins) to 4:00 PM (960 mins)
    if minute_of_day < 9 * 60 + 25:
        return SessionClockPhase.PREMARKET
    elif minute_of_day < 9 * 60 + 30:
        return SessionClockPhase.AUCTION_CONTEXT
    elif minute_of_day < 9 * 60 + 35:
        return SessionClockPhase.OPEN_0_5
    elif minute_of_day < 10 * 60:
        return SessionClockPhase.OPEN_5_30
    elif minute_of_day < 11 * 60 + 30:
        return SessionClockPhase.MORNING
    elif minute_of_day < 14 * 60:
        return SessionClockPhase.MIDDAY
    elif minute_of_day < 15 * 60:
        return SessionClockPhase.AFTERNOON
    elif minute_of_day < 15 * 60 + 45:
        return SessionClockPhase.POWER_HOUR
    else:
        return SessionClockPhase.CLOSE_WINDOW


def classify_market_behavior(spy_df: pd.DataFrame, ts: datetime) -> MarketBehaviorState:
    """Classify the market dynamics/behavior using SPY data up to the current timestamp.

    Causal: Only observes index <= ts.
    """
    if spy_df is None or spy_df.empty:
        return MarketBehaviorState.PRICE_DISCOVERY

    # Filter to look back up to current timestamp ts
    sub = spy_df[spy_df.index <= ts]
    if len(sub) < 30:
        return MarketBehaviorState.PRICE_DISCOVERY

    # Kaufman Efficiency Ratio of SPY over 20 bars
    # Compute on subset to be strictly causal
    close = sub["close"]
    
    # 20-bar ER
    er_series = efficiency_ratio(close, 20)
    er_spy = float(er_series.iloc[-1]) if len(er_series) >= 20 and pd.notna(er_series.iloc[-1]) else 0.25

    # Volatility percentile (using ATR percentage over last 390 bars)
    atr_series = atr(sub, 14)
    atr_pct_series = (atr_series / close * 100.0).tail(390)
    current_atr_pct = atr_pct_series.iloc[-1] if not atr_pct_series.empty else None
    
    if current_atr_pct is not None and pd.notna(current_atr_pct):
        vol_percentile = float((atr_pct_series <= current_atr_pct).mean())
    else:
        vol_percentile = 0.5

    # VWAP cross count over last 30 bars
    vwap_series = session_vwap(sub)
    cross_count = 0
    if len(sub) >= 30:
        recent_close = close.tail(30).to_numpy()
        recent_vwap = vwap_series.tail(30).to_numpy()
        # Count sign changes of difference
        diff = recent_close - recent_vwap
        for k in range(1, len(diff)):
            if pd.notna(diff[k]) and pd.notna(diff[k-1]) and (diff[k] * diff[k-1] < 0):
                cross_count += 1

    # EMA 20 slope over last 10 bars
    ema20 = ema(close, 20)
    slope = 0.0
    if len(ema20) >= 10:
        recent_ema = ema20.tail(10)
        if pd.notna(recent_ema.iloc[-1]) and pd.notna(recent_ema.iloc[-10]):
            slope = float((recent_ema.iloc[-1] / recent_ema.iloc[-10] - 1.0) * 100.0)

    # Classification logic
    clock_phase = classify_clock_phase(ts)

    if vol_percentile >= 0.85:
        return MarketBehaviorState.VOLATILITY_SHOCK
    elif cross_count > 4:
        return MarketBehaviorState.CHOP
    elif er_spy > 0.4 and abs(slope) > 0.05:
        if clock_phase in (SessionClockPhase.OPEN_0_5, SessionClockPhase.OPEN_5_30):
            return MarketBehaviorState.TREND_EXPANSION
        else:
            return MarketBehaviorState.TREND_CONTINUATION
    elif er_spy < 0.2 and vol_percentile < 0.3:
        return MarketBehaviorState.COMPRESSION
    elif er_spy < 0.2:
        return MarketBehaviorState.RANGE
    else:
        return MarketBehaviorState.PRICE_DISCOVERY
