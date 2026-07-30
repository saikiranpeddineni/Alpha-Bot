from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SessionContext:
    minutes_from_open: int | None
    minutes_to_close: int | None
    opening_phase: bool
    closing_phase: bool
    midday: bool


def session_context(now: datetime) -> SessionContext:
    local = now.astimezone(ET)
    if local.weekday() >= 5:
        return SessionContext(None, None, False, False, False)
    open_dt = local.replace(hour=9, minute=30, second=0, microsecond=0)
    close_dt = local.replace(hour=16, minute=0, second=0, microsecond=0)
    if local < open_dt or local > close_dt:
        return SessionContext(None, None, False, False, False)
    from_open = int((local - open_dt).total_seconds() // 60)
    to_close = int((close_dt - local).total_seconds() // 60)
    return SessionContext(
        minutes_from_open=from_open,
        minutes_to_close=to_close,
        opening_phase=from_open <= 45,
        closing_phase=to_close <= 30,
        midday=time(11, 30) <= local.time() <= time(14, 0),
    )
