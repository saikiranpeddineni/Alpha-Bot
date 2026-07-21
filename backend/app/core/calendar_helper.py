"""Market-session calendar helpers used by intraday replay (#231).

Session boundaries are resolved from the XNYS exchange calendar rather than a
hand-maintained holiday/early-close table.  This keeps replay evidence inside
the actual trading session, including holidays and year-specific early closes.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

ET = ZoneInfo("America/New_York")
_XNYS = xcals.get_calendar("XNYS")


def get_session_close(entry_dt: datetime) -> datetime:
    """Return the authoritative XNYS session close for ``entry_dt`` (#231).

    ``entry_dt`` may be naive (treated as UTC) or timezone-aware.  A timestamp
    whose Eastern calendar date is not an exchange session fails closed with
    ``ValueError`` instead of inventing a 16:00 close.
    """
    if entry_dt.tzinfo is None:
        from datetime import UTC

        entry_dt = entry_dt.replace(tzinfo=UTC)

    et_dt = entry_dt.astimezone(ET)
    session_date = et_dt.date().isoformat()

    if not _XNYS.is_session(session_date):
        raise ValueError(f"{session_date} is not an XNYS trading session")

    close_ts = _XNYS.session_close(session_date)
    close_dt = close_ts.to_pydatetime()
    return close_dt.astimezone(ET)
