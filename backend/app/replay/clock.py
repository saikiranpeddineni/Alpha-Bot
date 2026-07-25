"""ReplayClock -- Deterministic time adapter for TradingEngine during replay."""

from datetime import UTC, datetime
from app.core.clock import MarketClock


class ReplayClock(MarketClock):
    """Deterministic clock adapter wrapping MarketClock.

    Allows exact, controllable simulated time progression without
    modifying any production clock interface or method signatures.
    """

    def __init__(self, start_dt: datetime | None = None, always_open: bool = False) -> None:
        super().__init__(always_open=always_open)
        self._current_time: datetime = start_dt or datetime.now(UTC)
        if self._current_time.tzinfo is None:
            self._current_time = self._current_time.replace(tzinfo=UTC)

    def set_time(self, dt: datetime) -> None:
        """Set current simulated UTC time."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        self._current_time = dt

    def now_utc(self) -> datetime:
        """Override now_utc to return simulated time."""
        return self._current_time
