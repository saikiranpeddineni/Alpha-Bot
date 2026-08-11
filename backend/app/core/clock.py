"""U.S. equity market clock (America/New_York sessions).

In demo mode the market is always open so the platform is explorable at any
hour. Holiday handling covers fixed-date NYSE holidays; for weekends and
after-hours the clock is exact.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Fixed-date NYSE full-day holidays (observed dates shift; this covers the
# common ones -- brokers/data providers are the authority for live trading).
_FIXED_HOLIDAYS: set[tuple[int, int]] = {
    (1, 1),    # New Year's Day
    (6, 19),   # Juneteenth
    (7, 4),    # Independence Day
    (12, 25),  # Christmas
}


class MarketClock:
    def __init__(self, always_open: bool = False) -> None:
        self.always_open = always_open

    @staticmethod
    def now_utc() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def to_et(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            # Enforce UTC for any naive datetime to prevent system-local timezone corruption
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ET)

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        return (d.month, d.day) not in _FIXED_HOLIDAYS

    def is_open(self, dt: datetime | None = None) -> bool:
        if self.always_open:
            return True
        et = self.to_et(dt or self.now_utc())
        if not self.is_trading_day(et.date()):
            return False
        return MARKET_OPEN <= et.time() < MARKET_CLOSE

    def session_close(self, dt: datetime | None = None) -> datetime:
        et = self.to_et(dt or self.now_utc())
        return et.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0)

    def minutes_to_close(self, dt: datetime | None = None) -> float:
        """Minutes until today's close; large sentinel when always_open."""
        if self.always_open:
            return 390.0
        et = self.to_et(dt or self.now_utc())
        delta = self.session_close(et) - et
        return delta.total_seconds() / 60.0

    def in_entry_window(self, no_entry_minutes_before_close: int, dt: datetime | None = None) -> bool:
        """True when new positions may still be opened."""
        if not self.is_open(dt):
            return False
        if self.always_open:
            return True
        return self.minutes_to_close(dt) > no_entry_minutes_before_close

    def in_flatten_window(self, flatten_minutes_before_close: int, dt: datetime | None = None) -> bool:
        """True when all positions must be closed for the day."""
        if self.always_open:
            return False
        if not self.is_open(dt):
            return False
        return self.minutes_to_close(dt) <= flatten_minutes_before_close

    def next_open(self, dt: datetime | None = None) -> datetime:
        et = self.to_et(dt or self.now_utc())
        candidate = et.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)
        if et.time() >= MARKET_OPEN or not self.is_trading_day(et.date()):
            candidate += timedelta(days=1)
        while not self.is_trading_day(candidate.date()):
            candidate += timedelta(days=1)
        return candidate

    def session_date(self, dt: datetime | None = None) -> date:
        return self.to_et(dt or self.now_utc()).date()

    def status(self, dt: datetime | None = None) -> dict:
        dt = dt or self.now_utc()
        open_now = self.is_open(dt)
        return {
            "is_open": open_now,
            "session_date": self.session_date(dt).isoformat(),
            "minutes_to_close": round(self.minutes_to_close(dt), 1) if open_now else None,
            "next_open": None if open_now else self.next_open(dt).isoformat(),
            "always_open": self.always_open,
        }
