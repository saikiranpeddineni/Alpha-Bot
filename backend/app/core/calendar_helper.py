"""Market Session Exchange Calendar Helper (#231).

Resolves session close datetime for a given entry timestamp, handling regular trading
sessions (16:00 ET close) and known early-close dates (13:00 ET close).
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Known early close dates (Month, Day) -> Close Time (13:00 ET)
EARLY_CLOSE_DATES: set[tuple[int, int]] = {
    (7, 3),   # Day before Independence Day
    (11, 27), # Day after Thanksgiving
    (12, 24), # Christmas Eve
}


def get_session_close(entry_dt: datetime) -> datetime:
    """Return exchange session close datetime in Eastern Time for the given entry timestamp (#231).
    
    Session close is 16:00 ET on standard trading days, and 13:00 ET on early close dates.
    """
    if entry_dt.tzinfo is None:
        from datetime import UTC
        entry_dt = entry_dt.replace(tzinfo=UTC)

    et_dt = entry_dt.astimezone(ET)
    month_day = (et_dt.month, et_dt.day)

    close_hour = 13 if month_day in EARLY_CLOSE_DATES else 16
    session_close_et = datetime.combine(
        et_dt.date(),
        time(close_hour, 0, 0),
        tzinfo=ET
    )

    return session_close_et
