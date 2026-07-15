from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data.base import CachePolicy, HistoricalDataCoverageError
from app.data.manager import DataManager

ET = ZoneInfo("America/New_York")
COLUMNS = ["open", "high", "low", "close", "volume"]


class BlockingProvider:
    name = "blocking"

    def __init__(self) -> None:
        self.called = False

    async def get_bars(self, *args, **kwargs):
        self.called = True
        raise RuntimeError("NETWORK BLOCKED")

    async def get_quotes(self, symbols):
        return {}

    async def close(self):
        return None


def session_frame(day: str, close: str = "16:00") -> pd.DataFrame:
    index = pd.date_range(
        pd.Timestamp(f"{day} 09:30", tz=ET),
        pd.Timestamp(f"{day} {close}", tz=ET),
        freq="1min",
        inclusive="left",
    ).tz_convert(UTC)
    return pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in index],
        columns=COLUMNS,
        index=pd.DatetimeIndex(index, name="ts"),
    )


def requested_day(day: str) -> tuple[datetime, datetime]:
    start = pd.Timestamp(f"{day} 00:00", tz=ET).tz_convert(UTC).to_pydatetime()
    end = pd.Timestamp(f"{day} 23:59", tz=ET).tz_convert(UTC).to_pydatetime()
    return start, end


@pytest.mark.asyncio
async def test_one_bar_only_requested_session_fails_closed_without_provider_call(tmp_path):
    provider = BlockingProvider()
    dm = DataManager(provider, tmp_path / "bars.db")
    dm.cache_policy = CachePolicy.IMMUTABLE_CACHE_ONLY
    one_bar = session_frame("2026-07-06").iloc[[0]]
    dm.cache.upsert("SPY", "1Min", one_bar)
    start, end = requested_day("2026-07-06")

    with pytest.raises(HistoricalDataCoverageError, match="does not reach requested/session boundaries"):
        await dm.history("SPY", "1Min", start, end)

    assert not provider.called


@pytest.mark.asyncio
async def test_material_internal_minute_gap_fails_closed(tmp_path):
    provider = BlockingProvider()
    dm = DataManager(provider, tmp_path / "bars.db")
    dm.cache_policy = CachePolicy.IMMUTABLE_CACHE_ONLY
    frame = session_frame("2026-07-06")
    gap_start = pd.Timestamp("2026-07-06 12:00", tz=ET).tz_convert(UTC)
    gap_end = pd.Timestamp("2026-07-06 12:10", tz=ET).tz_convert(UTC)
    frame = frame[(frame.index < gap_start) | (frame.index >= gap_end)]
    dm.cache.upsert("SPY", "1Min", frame)
    start, end = requested_day("2026-07-06")

    with pytest.raises(HistoricalDataCoverageError, match="material internal gap of 10 consecutive minutes"):
        await dm.history("SPY", "1Min", start, end)

    assert not provider.called


@pytest.mark.asyncio
async def test_complete_normal_session_passes_immutable_coverage(tmp_path):
    provider = BlockingProvider()
    dm = DataManager(provider, tmp_path / "bars.db")
    dm.cache_policy = CachePolicy.IMMUTABLE_CACHE_ONLY
    frame = session_frame("2026-07-06")
    dm.cache.upsert("SPY", "1Min", frame)
    start, end = requested_day("2026-07-06")

    result = await dm.history("SPY", "1Min", start, end)

    assert len(result) == 390
    assert not provider.called


@pytest.mark.asyncio
async def test_black_friday_shortened_session_passes_with_210_bars(tmp_path):
    provider = BlockingProvider()
    dm = DataManager(provider, tmp_path / "bars.db")
    dm.cache_policy = CachePolicy.IMMUTABLE_CACHE_ONLY
    frame = session_frame("2026-11-27", close="13:00")
    dm.cache.upsert("SPY", "1Min", frame)
    start, end = requested_day("2026-11-27")

    result = await dm.history("SPY", "1Min", start, end)

    assert len(result) == 210
    assert result.index[-1].tz_convert(ET).time().isoformat() == "12:59:00"
    assert not provider.called
