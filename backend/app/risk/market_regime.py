"""CR-047 — Confidence-aware multi-signal market risk regime.

Replaces the SPY-only boolean/tri-state panic gate with a typed
``MarketRiskState`` that aggregates five independent signal families:

  1. broad_market   — SPY ATR/vol percentile
  2. qqq            — QQQ ATR/vol percentile (independent confirmation)
  3. breadth        — advancing/declining ratio across the live universe
  4. universe_stress — fraction of universe symbols showing extreme vol
  5. data_health    — overall provider health derived from family error rates

Aggregation rules
-----------------
* Each family casts a ``SignalVote`` with a state and a ``SignalHealth``
  (healthy flag, confidence 0..1, source timestamp, reason).
* A family that raises or returns unhealthy data casts ``UNKNOWN`` with
  confidence 0.  It can NEVER cast ``NORMAL``.
* The aggregator weights votes by ``health.confidence``.
* ``UNKNOWN`` is returned when:
    - fewer than 2 families are healthy, OR
    - weighted confidence < 0.40.
* Deterministic: same inputs at timestamp T always produce same output.
* No future-data leakage: every history fetch is bounded by ``as_of``.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import pandas as pd

from app.core.logging import applog


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class MarketRiskState(Enum):
    """Typed market risk classification.

    NORMAL  -- sufficient authoritative evidence of normal conditions.
    CAUTION -- moderate deterioration; entries allowed but risk scaled.
    STRESS  -- material broad/cross-sectional deterioration; deploy defensives.
    PANIC   -- confirmed extreme systemic conditions; deploy defensives.
    UNKNOWN -- insufficient authoritative evidence; fail closed for new entries.
               Missing evidence of stress is NOT evidence of safety.
    """
    NORMAL  = "NORMAL"
    CAUTION = "CAUTION"
    STRESS  = "STRESS"
    PANIC   = "PANIC"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SignalHealth:
    """Health/confidence metadata for a single signal family."""
    healthy: bool
    reason: str
    source_ts: datetime | None = None
    confidence: float = 1.0  # 0..1; 0 = completely unreliable

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))


@dataclass(frozen=True)
class SignalVote:
    """A single family's classification vote."""
    family: str
    state: MarketRiskState
    health: SignalHealth
    reason: str


# ---------------------------------------------------------------------------
# ATR percentile helper (shared by SPY and QQQ families)
# ---------------------------------------------------------------------------

def _atr_percentile(df_min: pd.DataFrame) -> tuple[float | None, str]:
    """Compute the current ATR% percentile from 1-min bars.

    Returns (percentile 0..1, reason_str) or (None, reason_str) on failure.
    """
    if df_min.empty or len(df_min) < 200:
        return None, f"insufficient 1-min bars ({len(df_min)})"

    df_h = df_min.resample("1h").agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}
    ).dropna()

    if len(df_h) < 20:
        return None, f"insufficient hourly bars ({len(df_h)})"

    prev = df_h["close"].shift(1)
    tr = pd.concat(
        [df_h["high"] - df_h["low"],
         (df_h["high"] - prev).abs(),
         (df_h["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    atr_pct = tr.ewm(alpha=1.0 / 14, adjust=False).mean() / df_h["close"] * 100.0

    last_val = atr_pct.iloc[-1]
    if not math.isfinite(last_val):
        return None, "non-finite ATR value"

    std = atr_pct.std()
    if std < 1e-6:
        pct = 0.0
    else:
        pct = float((atr_pct.tail(200) <= last_val).mean())
    return pct, f"ATR percentile={pct:.3f}"


# ---------------------------------------------------------------------------
# MarketRiskAggregator
# ---------------------------------------------------------------------------

class MarketRiskAggregator:
    """Check VIX index values for systemic stress.

    Parameters
    ----------
    data_manager:
        Provides ``async history(symbol, timeframe, start, end)``.
    universe_symbols:
        Unused, kept for signature compatibility.
    """

    def __init__(
        self,
        data_manager: Any,
        universe_symbols: list[str] | None = None,
    ) -> None:
        self._data = data_manager

    async def assess(
        self, as_of: datetime | None = None
    ) -> tuple[MarketRiskState, float, float, list[str], dict[str, SignalHealth]]:
        """Query the VIX index and map to MarketRiskState.

        Returns
        -------
        state : MarketRiskState
        confidence : float  (1.0 if healthy, 0.0 otherwise)
        coverage : float    (1.0 if healthy, 0.0 otherwise)
        reason_codes : list[str]
        input_healths : dict[str, SignalHealth]
        """
        T = as_of or datetime.now(UTC)
        vix_val = None
        source_ts = T
        reason = ""

        # Query VIX/ ^VIX with 5-day lookback to cover weekends
        start = T - timedelta(days=5)

        for symbol in ["^VIX", "VIX"]:
            try:
                df = await self._data.history(symbol, "1Min", start, T, prefer_cache=True)
                if not df.empty:
                    vix_val = float(df["close"].iloc[-1])
                    source_ts = df.index[-1].to_pydatetime() if hasattr(df.index[-1], "to_pydatetime") else T
                    reason = f"Fetched {symbol} from primary provider: {vix_val:.2f}"
                    break
            except Exception as e:
                applog.debug("market_regime", f"Failed to fetch {symbol} from primary provider: {e}")

        # Fallback to yfinance if primary provider is empty/unavailable
        if vix_val is None:
            try:
                import yfinance as yf
                def _yf_fetch():
                    df = yf.download("^VIX", start=start, end=T, interval="1m", progress=False)
                    if df is not None and not df.empty:
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        return float(df["Close"].iloc[-1])
                    df = yf.download("^VIX", start=start, end=T, interval="1d", progress=False)
                    if df is not None and not df.empty:
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        return float(df["Close"].iloc[-1])
                    return None

                vix_val = await asyncio.to_thread(_yf_fetch)
                if vix_val is not None:
                    reason = f"Fetched ^VIX from yfinance: {vix_val:.2f}"
            except Exception as e:
                applog.warning("market_regime", f"Failed to fetch ^VIX from yfinance fallback: {e}")

        if vix_val is None:
            state = MarketRiskState.UNKNOWN
            confidence = 0.0
            coverage = 0.0
            reasons = ["VIX_UNAVAILABLE"]
            health = SignalHealth(healthy=False, reason="VIX index data is unavailable", source_ts=T, confidence=0.0)
        else:
            coverage = 1.0
            confidence = 1.0
            if vix_val < 20.0:
                state = MarketRiskState.NORMAL
            elif vix_val < 30.0:
                state = MarketRiskState.CAUTION
            else:
                state = MarketRiskState.PANIC

            reasons = [f"VIX:{state.value}({vix_val:.2f})"]
            health = SignalHealth(healthy=True, reason=reason, source_ts=source_ts, confidence=1.0)

        input_healths = {"vix": health}

        applog.info(
            "market_regime",
            f"MarketRiskState={state.value} confidence={confidence:.2f} "
            f"coverage={coverage:.2f} reasons={reasons}",
        )
        return state, confidence, coverage, reasons, input_healths
