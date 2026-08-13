"""Canonical Market Time and Session Boundary Logic.

Normalizes timestamps to US/Eastern timezone, handles DST, 
and provides strictly defined boundaries for market open/close.
"""

from datetime import datetime, time
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo # type: ignore

MARKET_TZ = ZoneInfo("America/New_York")

def to_market_time(ts: datetime) -> datetime:
    """Ensure a timestamp is converted correctly to America/New_York."""
    if ts.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware (UTC or other)")
    return ts.astimezone(MARKET_TZ)

def regular_session_start(session_date: datetime) -> datetime:
    """Get exact 09:30 ET start for a given date."""
    return datetime.combine(
        session_date.date() if hasattr(session_date, "date") else session_date,
        time(9, 30),
        tzinfo=MARKET_TZ,
    )

def regular_session_end(session_date: datetime) -> datetime:
    """Get exact 16:00 ET end for a given date."""
    return datetime.combine(
        session_date.date() if hasattr(session_date, "date") else session_date,
        time(16, 0),
        tzinfo=MARKET_TZ,
    )
