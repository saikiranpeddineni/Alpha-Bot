"""Market Session Exchange Calendar Helper (#231).

Resolves session close datetime for a given entry timestamp, handling regular trading
sessions (16:00 ET close) and exchange early-close dates (13:00 ET close).
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

ET = ZoneInfo("America/New_York")

# Known early close dates (Month, Day) -> Close Time (13:00 ET) - fallback & explicit overrides
EARLY_CLOSE_DATES: set[tuple[int, int]] = {
    (7, 3),   # Day before Independence Day
    (11, 27), # Day after Thanksgiving (2025/2026 Black Friday)
    (12, 24), # Christmas Eve
}


def get_early_close_dates_for_year(year: int) -> set[tuple[int, int]]:
    """Dynamically compute US equity exchange early close dates for a given year using exchange rules."""
    early_closes: set[tuple[int, int]] = set(EARLY_CLOSE_DATES)

    # 1. Day before Independence Day (July 3), if July 4 is a weekday
    july4 = datetime(year, 7, 4)
    if july4.weekday() in (1, 2, 3, 4):  # Tue-Fri
        early_closes.add((7, 3))
    elif july4.weekday() == 0:  # Mon (Independence Day observed July 4 Mon, July 3 is Sunday)
        early_closes.add((7, 1))

    # 2. Black Friday (Day after Thanksgiving, 4th Thursday in November)
    thursdays = [d for d in range(1, 31) if datetime(year, 11, d).weekday() == 3]
    if len(thursdays) >= 4:
        thanksgiving_day = thursdays[3]
        black_friday = thanksgiving_day + 1
        early_closes.add((11, black_friday))

    # 3. Christmas Eve (December 24), if Christmas (Dec 25) is a weekday
    dec25 = datetime(year, 12, 25)
    if dec25.weekday() in (1, 2, 3, 4, 5):  # Tue-Sat
        early_closes.add((12, 24))

    return early_closes


def get_session_close(entry_dt: datetime) -> datetime:
    """Return exchange session close datetime in Eastern Time for the given entry timestamp (#231).
    
    Session close is 16:00 ET on standard trading days, and 13:00 ET on early close dates.
    """
    if entry_dt.tzinfo is None:
        from datetime import UTC
        entry_dt = entry_dt.replace(tzinfo=UTC)

    et_dt = entry_dt.astimezone(ET)
    early_closes = get_early_close_dates_for_year(et_dt.year)
    month_day = (et_dt.month, et_dt.day)

    close_hour = 13 if month_day in early_closes else 16
    session_close_et = datetime.combine(
        et_dt.date(),
        time(close_hour, 0, 0),
        tzinfo=ET
    )

    return session_close_et
