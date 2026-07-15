"""Market data provider abstraction.

Add a new data source by subclassing DataProvider and registering it in
app.data.factory. All providers return the canonical bar DataFrame:
tz-aware UTC DatetimeIndex + open/high/low/close/volume columns.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import pandas as pd

from app.core.domain import Quote

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]

TIMEFRAMES: dict[str, timedelta] = {
    "1Min": timedelta(minutes=1),
    "5Min": timedelta(minutes=5),
    "15Min": timedelta(minutes=15),
    "1Day": timedelta(days=1),
}


def timeframe_delta(timeframe: str) -> timedelta:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; expected one of {list(TIMEFRAMES)}")
    return TIMEFRAMES[timeframe]


def empty_bars() -> pd.DataFrame:
    df = pd.DataFrame(columns=BAR_COLUMNS)
    df.index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return df


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce an arbitrary OHLCV frame into the canonical shape."""
    if df is None or df.empty:
        return empty_bars()
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    out = out[[c for c in BAR_COLUMNS if c in out.columns]]
    for col in BAR_COLUMNS:
        if col not in out.columns:
            out[col] = 0.0
    out = out[BAR_COLUMNS].astype(float)
    idx = pd.DatetimeIndex(out.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    out.index = idx
    out.index.name = "ts"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def resample_bars(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate 1Min bars up to a coarser timeframe."""
    if df.empty or timeframe == "1Min":
        return df
    delta = timeframe_delta(timeframe)
    agg = df.resample(delta, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return agg.dropna(subset=["open"])


class DataProvider(ABC):
    """Abstract market data source."""

    name: str = "abstract"

    @abstractmethod
    async def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Min",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Historical/latest bars in canonical form."""

    @abstractmethod
    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Latest price snapshot per symbol."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class HistoricalDataCoverageError(Exception):
    def __init__(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        cached_start: datetime | None,
        cached_end: datetime | None,
        reason: str,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.start = start
        self.end = end
        self.cached_start = cached_start
        self.cached_end = cached_end
        self.reason = reason
        super().__init__(
            f"Coverage check failed for {symbol} ({timeframe}): {reason}. "
            f"Requested: {start} -> {end}. "
            f"Cached: {cached_start} -> {cached_end}."
        )


class CachePolicy:
    DEFAULT = "default"
    IMMUTABLE_CACHE_ONLY = "immutable_cache_only"


def filter_session_view(df: pd.DataFrame, view: str) -> pd.DataFrame:
    """Filter a DatetimeIndex-indexed DataFrame (UTC) into explicit session views.

    Supported views: 'PREMARKET', 'REGULAR_SESSION', 'POSTMARKET', 'ALL_AVAILABLE'
    """
    if df.empty:
        return df

    from zoneinfo import ZoneInfo
    et_idx = df.index.tz_convert(ZoneInfo("America/New_York"))
    minutes = et_idx.hour * 60 + et_idx.minute

    if view == "PREMARKET":
        mask = (minutes >= 4 * 60) & (minutes < 9 * 60 + 30)
    elif view == "REGULAR_SESSION":
        mask = (minutes >= 9 * 60 + 30) & (minutes < 16 * 60)
    elif view == "POSTMARKET":
        mask = (minutes >= 16 * 60) & (minutes < 20 * 60)
    elif view == "ALL_AVAILABLE":
        return df
    else:
        raise ValueError(f"Unknown session view: {view!r}")

    return df[mask]
