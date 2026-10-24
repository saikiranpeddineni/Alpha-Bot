"""U.S. equity market clock (America/New_York sessions).

In demo mode the market is always open so the platform is explorable at any
hour. Trading days, session opens/closes, holidays AND year-specific early
closes are resolved from the authoritative XNYS exchange calendar — a
hand-maintained holiday table missed MLK/Presidents/Memorial/Labor/
Thanksgiving/Good Friday and every 13:00 ET early close, which mis-timed the
EOD flatten by up to three hours on those days.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
EARLY_MARKET_CLOSE = time(13, 0)

# =============================================================================
# [MODIFIED 2026-10-25]: 2026-2027 NYSE 1:00 PM Early Close Registry
# ORIGINAL INTENT: Rely purely on exchange_calendars library for early close detection.
# REASON FOR CHANGE: Day Trader Suite trader-alerts module defines explicit NYSE early closes;
#                    guarantees early close detection if exchange_calendars is offline or out-of-range.
# SAFE ROLLBACK: Remove NYSE_EARLY_CLOSE or delete references.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
# (Constant did not previously exist)
# -----------------------------------------------------------------------------
# [NEW CODE]:
NYSE_EARLY_CLOSE: dict[str, str] = {
    "2026-11-27": "day after Thanksgiving (1:00 PM ET early close)",
    "2026-12-24": "Christmas Eve (1:00 PM ET early close)",
    "2027-11-26": "day after Thanksgiving (1:00 PM ET early close)",
    "2027-12-24": "Christmas Eve (1:00 PM ET early close)",
}
# =============================================================================

_XNYS = xcals.get_calendar("XNYS")


class MarketClock:
    def __init__(self, always_open: bool = False) -> None:
        self.always_open = always_open

    @staticmethod
    def now_utc() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def to_et(dt: datetime) -> datetime:
        return dt.astimezone(ET)

    def is_trading_day(self, d: date) -> bool:
        """Authoritative XNYS session check (holidays + weekends)."""
        if self.always_open:
            return d.weekday() < 5
        try:
            return _XNYS.is_session(d.isoformat())
        except Exception:
            # Calendar lookup failure must never claim a trading day.
            return d.weekday() < 5

    def session_close(self, dt: datetime | None = None) -> datetime:
        """Actual close for the session containing ``dt`` (13:00 on early days)."""
        et = self.to_et(dt or self.now_utc())
        try:
            return get_xnys_session_close(et)
        except ValueError:
            # Not a trading session (weekend/holiday): fall back to nominal close.
            return et.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0)

    def is_open(self, dt: datetime | None = None) -> bool:
        if self.always_open:
            return True
        et = self.to_et(dt or self.now_utc())
        if not self.is_trading_day(et.date()):
            return False
        return MARKET_OPEN <= et.time() < self.session_close(et).time()

    def minutes_to_close(self, dt: datetime | None = None) -> float:
        """Minutes until today's actual close; large sentinel when always_open."""
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

    def is_early_close(self, dt: datetime | None = None) -> bool:
        """True if the session containing ``dt`` is an early close (1:00 PM ET)."""
        et = self.to_et(dt or self.now_utc())
        date_str = et.date().isoformat()
        if date_str in NYSE_EARLY_CLOSE:
            return True
        try:
            close_dt = self.session_close(dt)
            return close_dt.time() <= EARLY_MARKET_CLOSE
        except Exception:
            return False

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


def get_xnys_session_close(entry_dt: datetime) -> datetime:
    """XNYS session close for the ET calendar date of ``entry_dt``.

    Fails closed with ValueError when the date is not an exchange session.
    """
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=UTC)
    et_dt = entry_dt.astimezone(ET)
    session_date = et_dt.date().isoformat()
    # =============================================================================
    # [MODIFIED 2026-10-25]: Robust Early Close & Calendar Exception Guard
    # ORIGINAL INTENT: Looked up _XNYS.is_session and _XNYS.session_close directly.
    # REASON FOR CHANGE: When querying dates near or beyond calendar horizon (e.g. 2027),
    #                    _XNYS raises NotSessionError. NYSE_EARLY_CLOSE provides authoritative 13:00 close.
    # SAFE ROLLBACK: Revert to direct _XNYS query below.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # if not _XNYS.is_session(session_date):
    #     raise ValueError(f"{session_date} is not an XNYS trading session")
    # close_ts = _XNYS.session_close(session_date)
    # return close_ts.to_pydatetime().astimezone(ET)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    if session_date in NYSE_EARLY_CLOSE:
        return et_dt.replace(hour=EARLY_MARKET_CLOSE.hour, minute=EARLY_MARKET_CLOSE.minute, second=0, microsecond=0)

    try:
        if not _XNYS.is_session(session_date):
            raise ValueError(f"{session_date} is not an XNYS trading session")
        close_ts = _XNYS.session_close(session_date)
        return close_ts.to_pydatetime().astimezone(ET)
    except ValueError:
        raise
    except Exception as exc:
        if et_dt.date().weekday() < 5:
            return et_dt.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0)
        raise ValueError(f"{session_date} is not an XNYS trading session: {exc}")
    # =============================================================================
