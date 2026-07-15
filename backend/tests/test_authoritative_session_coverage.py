from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data.base import CachePolicy, HistoricalDataCoverageError
from app.data.manager import DataManager
from app.data.session_coverage import EXCHANGE_CALENDAR_SOURCE, _expected_sessions

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


def day_bounds(day: str) -> tuple[datetime, datetime]:
    start = pd.Timestamp(f"{day} 00:00", tz=ET).tz_convert(UTC).to_pydatetime()
    end = pd.Timestamp(f"{day} 23:59", tz=ET).tz_convert(UTC).to_pydatetime()
    return start, end


def frame_from_expected(day: str) -> pd.DataFrame:
    start, end = day_bounds(day)
    sessions = _expected_sessions(start, end)
    assert len(sessions) == 1
    _, expected_et = sessions[0]
    index = expected_et.tz_convert(UTC)
    return pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in index],
        columns=COLUMNS,
        index=pd.DatetimeIndex(index, name="ts"),
    )


def test_calendar_source_is_explicit_and_versionable_dependency():
    assert EXCHANGE_CALENDAR_SOURCE == "exchange_calendars:XNYS"


def test_2018_national_day_of_mourning_is_closed():
    start, end = day_bounds("2018-12-05")
    assert _expected_sessions(start, end) == []


def test_adjacent_normal_session_still_requires_complete_coverage(tmp_path):
    provider = BlockingProvider()
    dm = DataManager(provider, tmp_path / "bars.db")
    dm.cache_policy = CachePolicy.IMMUTABLE_CACHE_ONLY
    start, end = day_bounds("2018-12-06")

    with pytest.raises(HistoricalDataCoverageError, match="2018-12-06"):
        dm.validate_cache_coverage("SPY", "1Min", start, end, pd.DataFrame(columns=COLUMNS))

    assert not provider.called


def test_recurring_holiday_remains_closed():
    start, end = day_bounds("2026-12-25")
    assert _expected_sessions(start, end) == []


def test_black_friday_schedule_has_210_expected_minutes():
    start, end = day_bounds("2026-11-27")
    sessions = _expected_sessions(start, end)
    assert len(sessions) == 1
    assert len(sessions[0][1]) == 210
    assert sessions[0][1][-1].time().isoformat() == "12:59:00"


@pytest.mark.asyncio
async def test_exceptional_closure_requires_zero_bars_and_zero_provider_calls(tmp_path):
    provider = BlockingProvider()
    dm = DataManager(provider, tmp_path / "bars.db")
    dm.cache_policy = CachePolicy.IMMUTABLE_CACHE_ONLY
    start, end = day_bounds("2018-12-05")

    result = await dm.history("SPY", "1Min", start, end)

    assert result.empty
    assert not provider.called


@pytest.mark.asyncio
async def test_complete_adjacent_session_passes_without_fabrication_or_provider_calls(tmp_path):
    provider = BlockingProvider()
    dm = DataManager(provider, tmp_path / "bars.db")
    dm.cache_policy = CachePolicy.IMMUTABLE_CACHE_ONLY
    frame = frame_from_expected("2018-12-06")
    dm.cache.upsert("SPY", "1Min", frame)
    start, end = day_bounds("2018-12-06")

    result = await dm.history("SPY", "1Min", start, end)

    assert len(result) == 390
    assert result.index.equals(frame.index)
    assert not provider.called
