"""Canonical datetime helpers for Alpha-Bot.

Trading/business timestamps are timezone-aware UTC.  Market-session conversion belongs
in MarketClock; UI/local conversion belongs at presentation boundaries.  These helpers
intentionally preserve live semantics while giving replay/tests one normalization point.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any


def utc_now() -> datetime:
    """Return the current wall-clock instant as timezone-aware UTC.

    Authoritative engine code should prefer its injected MarketClock/ReplayClock when
    one is available.  This helper is for components without a runtime clock and for
    operational/audit timestamps.
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime, *, naive_is_utc: bool = True) -> datetime:
    """Normalize a datetime to aware UTC without changing its represented instant.

    Legacy Alpha-Bot SQLite rows historically stored naive UTC.  For compatibility,
    naive values are interpreted as UTC by default rather than as machine-local time.
    """
    if value.tzinfo is None:
        if not naive_is_utc:
            raise ValueError("Naive datetime is not allowed")
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_utc(value: Any) -> datetime | None:
    """Parse common persisted/wire datetime forms into aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def utc_date(now: datetime | None = None) -> date:
    """Return a UTC calendar date; do not use this for NYSE session dates."""
    return ensure_utc(now).date() if now is not None else utc_now().date()
