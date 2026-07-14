"""Economic Calendar and Event Guard for AlphaDesk.

Detects two types of high-impact events that degrade intraday technical signals:
  1. **Earnings releases**: Detected via Alpaca news feed keyword search for
     watchlist symbols (AMC/BMO patterns in headlines).
  2. **Macro events**: Known high-impact recurring indicators (CPI, FOMC, NFP, PPI,
     Retail Sales) detected by Alpaca news headline keywords within a time window.

When an event is active, the pipeline raises its Trade Score threshold by
``score_threshold_event`` (default: 75.0), requiring a higher-conviction setup
to enter. Trades are NOT hard-blocked; exceptional setups can still trade.

The event window is held active for ``EVENT_WINDOW_MINUTES`` minutes after
the last detection to cover post-announcement volatility.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.core.logging import applog

if TYPE_CHECKING:
    pass

# Minutes to hold the event-active flag after last detection
EVENT_WINDOW_MINUTES = 20

# Keywords in news headlines that indicate a high-impact macro event
MACRO_KEYWORDS = re.compile(
    r"\b("
    r"CPI|consumer price index|"
    r"FOMC|federal reserve|fed decision|rate decision|interest rate|"
    r"nonfarm payroll|NFP|jobs report|unemployment rate|"
    r"PPI|producer price|"
    r"retail sales|GDP|gross domestic product|"
    r"PCE|personal consumption|"
    r"ISM manufacturing|ISM services"
    r")\b",
    re.IGNORECASE,
)

# Keywords that indicate an earnings release for a watchlist stock
EARNINGS_KEYWORDS = re.compile(
    r"\b(earnings|EPS|quarterly results?|Q[1-4] results?|beats estimates?|"
    r"misses estimates?|revenue guidance|fiscal quarter)\b",
    re.IGNORECASE,
)


class EventCalendar:
    """Detects market-moving events from Alpaca news and guards the pipeline.

    Usage (from engine):
        calendar = EventCalendar(alpaca_client)
        is_active, reason = await calendar.check(now, watchlist)
        pipeline.set_event_day(is_active)
    """

    def __init__(self, alpaca_client=None) -> None:
        self._alpaca = alpaca_client
        self._event_until: datetime | None = None
        self._last_reason: str = ""

    def _extend_event_window(self, reason: str, now: datetime) -> None:
        """Mark an event as active and extend the guard window."""
        self._event_until = now + timedelta(minutes=EVENT_WINDOW_MINUTES)
        if self._last_reason != reason:
            self._last_reason = reason
            applog.info("calendar", f"Event guard ACTIVE: {reason} — threshold raised for {EVENT_WINDOW_MINUTES} min")

    def is_currently_active(self, now: datetime) -> tuple[bool, str]:
        """Check if the event window is still in effect."""
        if self._event_until and now < self._event_until:
            return True, self._last_reason
        return False, ""

    async def check(self, now: datetime, watchlist: list[str]) -> tuple[bool, str]:
        """Check for active events. Returns (is_active, reason).

        Priority order:
          1. If we're still inside an existing event window → stay active.
          2. Alpaca news check for macro keywords (last 5 min window).
          3. Alpaca news check for earnings keywords on watchlist symbols.
        """
        # 1. Still inside existing window?
        active, reason = self.is_currently_active(now)
        if active:
            return True, reason

        # 2 & 3. Query Alpaca news if client is available
        if self._alpaca is not None:
            try:
                await self._check_alpaca_news(now, watchlist)
            except Exception as exc:
                applog.debug("calendar", f"News check failed (non-critical): {exc}")

        return self.is_currently_active(now)

    async def _check_alpaca_news(self, now: datetime, watchlist: list[str]) -> None:
        """Query Alpaca news feed for macro and earnings headlines."""
        try:
            # Look back 5 minutes for breaking news
            start = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            # General macro news (any symbol)
            news_items = await self._fetch_news(symbols=None, start=start, end=end, limit=20)
            for item in news_items:
                headline = item.get("headline", "") + " " + item.get("summary", "")
                if MACRO_KEYWORDS.search(headline):
                    self._extend_event_window(f"macro event detected: {item.get('headline', '')[:60]}", now)
                    return

            # Earnings news for watchlist symbols
            if watchlist:
                earnings_items = await self._fetch_news(symbols=watchlist, start=start, end=end, limit=30)
                for item in earnings_items:
                    headline = item.get("headline", "") + " " + item.get("summary", "")
                    if EARNINGS_KEYWORDS.search(headline):
                        symbols_in = item.get("symbols", [])
                        sym_str = ", ".join(symbols_in[:3]) if symbols_in else "unknown"
                        self._extend_event_window(
                            f"earnings news detected ({sym_str}): {item.get('headline', '')[:50]}", now
                        )
                        return
        except Exception as exc:
            applog.debug("calendar", f"Alpaca news query error: {exc}")

    async def _fetch_news(
        self,
        symbols: list[str] | None,
        start: str,
        end: str,
        limit: int = 20,
    ) -> list[dict]:
        """Fetch news from Alpaca REST client. Returns list of news dicts."""
        try:
            # Alpaca SDK v3+ style
            client = self._alpaca
            params: dict = {"start": start, "end": end, "limit": limit, "sort": "desc"}
            if symbols:
                params["symbols"] = ",".join(symbols[:10])  # Alpaca API limit

            # Try async REST call (alpaca-py)
            import asyncio
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: client.get_news(**{k: v for k, v in params.items()}),
            )
            # Normalize to list of dicts
            if hasattr(resp, "__iter__"):
                result = []
                for item in resp:
                    if hasattr(item, "model_dump"):
                        result.append(item.model_dump())
                    elif hasattr(item, "__dict__"):
                        result.append(item.__dict__)
                    elif isinstance(item, dict):
                        result.append(item)
                return result
        except Exception:
            pass
        return []

    def deactivate(self) -> None:
        """Manually clear the event window (e.g., at session end)."""
        if self._event_until is not None:
            applog.info("calendar", "Event guard CLEARED")
        self._event_until = None
        self._last_reason = ""
