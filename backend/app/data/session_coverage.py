"""Market-session completeness checks for cached historical data.

The validator is deliberately independent of provider behavior: immutable replay
must prove that requested 1-minute regular-session data is materially complete
before strategy code can consume it. Missing bars are never synthesized.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
)

from app.data.base import HistoricalDataCoverageError

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
MIN_SESSION_COMPLETENESS = 0.95
MAX_INTERNAL_GAP_MINUTES = 5


class _NYSEFullDayHolidayCalendar(AbstractHolidayCalendar):
    """NYSE full-day holidays needed to resolve expected replay sessions."""

    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


def _holiday_dates(start: date, end: date) -> set[date]:
    holidays = _NYSEFullDayHolidayCalendar().holidays(start=start, end=end)
    return {ts.date() for ts in holidays}


def _thanksgiving(year: int) -> date:
    november_first = date(year, 11, 1)
    first_thursday = november_first + timedelta(days=(3 - november_first.weekday()) % 7)
    return first_thursday + timedelta(weeks=3)


def _is_early_close(session_date: date, holiday_dates: set[date]) -> bool:
    # Day after Thanksgiving.
    if session_date == _thanksgiving(session_date.year) + timedelta(days=1):
        return True

    # Last open weekday before Independence Day.
    july_fourth = date(session_date.year, 7, 4)
    july_candidate = july_fourth - timedelta(days=1)
    while july_candidate.weekday() >= 5 or july_candidate in holiday_dates:
        july_candidate -= timedelta(days=1)
    if session_date == july_candidate:
        return True

    # Christmas Eve when it is itself an open weekday.
    christmas_eve = date(session_date.year, 12, 24)
    return session_date == christmas_eve and session_date.weekday() < 5 and session_date not in holiday_dates


def _expected_sessions(start: datetime, end: datetime) -> list[tuple[date, pd.DatetimeIndex]]:
    start_et = pd.Timestamp(start).tz_convert(ET)
    end_et = pd.Timestamp(end).tz_convert(ET)
    holidays = _holiday_dates(start_et.date(), end_et.date())
    sessions: list[tuple[date, pd.DatetimeIndex]] = []

    current = start_et.date()
    while current <= end_et.date():
        if current.weekday() < 5 and current not in holidays:
            close = EARLY_CLOSE if _is_early_close(current, holidays) else REGULAR_CLOSE
            open_ts = pd.Timestamp(datetime.combine(current, SESSION_OPEN, tzinfo=ET))
            close_ts = pd.Timestamp(datetime.combine(current, close, tzinfo=ET))
            expected = pd.date_range(open_ts, close_ts, freq="1min", inclusive="left")
            expected = expected[(expected >= start_et) & (expected <= end_et)]
            if len(expected):
                sessions.append((current, expected))
        current += timedelta(days=1)
    return sessions


def _raise_coverage_error(
    *, symbol: str, timeframe: str, start: datetime, end: datetime,
    cached_df: pd.DataFrame, reason: str,
) -> None:
    raise HistoricalDataCoverageError(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        cached_start=None if cached_df.empty else cached_df.index[0].to_pydatetime(),
        cached_end=None if cached_df.empty else cached_df.index[-1].to_pydatetime(),
        reason=reason,
    )


def validate_requested_session_coverage(
    *, symbol: str, timeframe: str, start: datetime, end: datetime, cached_df: pd.DataFrame,
) -> None:
    """Validate requested market-session coverage without inventing missing bars.

    For 1-minute data, each requested regular-session slice must reach both
    requested/session boundaries, contain at least 95% of expected minute labels,
    and have no internal run of more than five missing minutes. Other timeframes
    retain session-presence validation.
    """
    sessions = _expected_sessions(start, end)
    if not sessions:
        return
    if cached_df.empty:
        _raise_coverage_error(
            symbol=symbol, timeframe=timeframe, start=start, end=end,
            cached_df=cached_df, reason="Cached dataset is empty",
        )

    et_index = cached_df.index.tz_convert(ET)
    cached_dates = set(et_index.date)
    for session_date, expected in sessions:
        if session_date not in cached_dates:
            _raise_coverage_error(
                symbol=symbol, timeframe=timeframe, start=start, end=end,
                cached_df=cached_df,
                reason=f"Cached data is missing completely for expected trading day {session_date}",
            )

        if timeframe != "1Min":
            continue

        actual = et_index[et_index.date == session_date]
        actual = actual[(actual >= expected[0]) & (actual <= expected[-1])]
        actual = pd.DatetimeIndex(actual.unique()).sort_values()
        expected_set = set(expected)
        actual_expected = pd.DatetimeIndex([ts for ts in actual if ts in expected_set])

        if len(actual_expected) == 0 or actual_expected[0] != expected[0] or actual_expected[-1] != expected[-1]:
            _raise_coverage_error(
                symbol=symbol, timeframe=timeframe, start=start, end=end,
                cached_df=cached_df,
                reason=(f"Cached 1-minute session {session_date} does not reach requested/session "
                        f"boundaries {expected[0]} through {expected[-1]}"),
            )

        completeness = len(actual_expected) / len(expected)
        if completeness < MIN_SESSION_COMPLETENESS:
            _raise_coverage_error(
                symbol=symbol, timeframe=timeframe, start=start, end=end,
                cached_df=cached_df,
                reason=(f"Cached 1-minute session {session_date} is materially partial: "
                        f"{len(actual_expected)}/{len(expected)} expected bars ({completeness:.1%})"),
            )

        missing = expected.difference(actual_expected)
        if len(missing):
            positions = expected.get_indexer(missing)
            longest_run = run = 1
            for previous, current in zip(positions, positions[1:]):
                run = run + 1 if current == previous + 1 else 1
                longest_run = max(longest_run, run)
            if longest_run > MAX_INTERNAL_GAP_MINUTES:
                _raise_coverage_error(
                    symbol=symbol, timeframe=timeframe, start=start, end=end,
                    cached_df=cached_df,
                    reason=(f"Cached 1-minute session {session_date} has a material internal gap "
                            f"of {longest_run} consecutive minutes"),
                )
