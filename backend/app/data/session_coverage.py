"""Market-session completeness checks for cached historical data.

Expected sessions come from the versioned XNYS exchange calendar supplied by
``exchange_calendars``. Immutable replay therefore validates against the actual
published historical exchange schedule, including one-off closures and special
closes, instead of inferring sessions from recurring weekday holidays.

Missing market bars are never synthesized.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from app.data.base import HistoricalDataCoverageError

ET = ZoneInfo("America/New_York")
EXCHANGE_CALENDAR = "XNYS"
EXCHANGE_CALENDAR_SOURCE = "exchange_calendars:XNYS"
MIN_SESSION_COMPLETENESS = 0.95
MAX_INTERNAL_GAP_MINUTES = 5


def _expected_sessions(start: datetime, end: datetime) -> list[tuple[date, pd.DatetimeIndex]]:
    """Return requested 1-minute labels from the authoritative XNYS schedule."""
    start_utc = pd.Timestamp(start).tz_convert("UTC")
    end_utc = pd.Timestamp(end).tz_convert("UTC")
    start_et = start_utc.tz_convert(ET)
    end_et = end_utc.tz_convert(ET)

    calendar = xcals.get_calendar(EXCHANGE_CALENDAR)
    session_labels = calendar.sessions_in_range(
        pd.Timestamp(start_et.date()),
        pd.Timestamp(end_et.date()),
    )
    sessions: list[tuple[date, pd.DatetimeIndex]] = []
    for label in session_labels:
        open_utc = pd.Timestamp(calendar.session_open(label)).tz_convert("UTC")
        close_utc = pd.Timestamp(calendar.session_close(label)).tz_convert("UTC")
        expected_utc = pd.date_range(open_utc, close_utc, freq="1min", inclusive="left")
        expected_utc = expected_utc[(expected_utc >= start_utc) & (expected_utc <= end_utc)]
        if len(expected_utc):
            sessions.append((pd.Timestamp(label).date(), expected_utc.tz_convert(ET)))
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

    For 1-minute data, each requested XNYS session slice must reach both
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
                reason=(f"Cached data is missing completely for expected {EXCHANGE_CALENDAR} "
                        f"trading day {session_date}"),
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
