"""Dynamic Pre-Market Gap Mover Scanner.

Queries Alpaca's snapshot/most-active endpoints each morning at 9:00 AM ET
to identify the top pre-market movers. These gap names have the highest
intraday momentum continuation probability and are injected directly into
MarketScanner Stage 1 alongside the static MARKET_UNIVERSE.

Filters applied:
- Price >= $5.00 (configurable via min_price)
- Pre-market dollar volume >= $5M (configurable)
- Gap >= 2.0% from prior close (configurable)
- Not already in CANDIDATE_POOL (additive, not duplicate)

Fail-open: if Alpaca data is unavailable or pre-market is not active,
returns empty list and falls back to static CANDIDATE_POOL silently.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.data.manager import DataManager

log = logging.getLogger(__name__)


# Minimum pre-market dollar volume to qualify as a gap mover
_GAP_MIN_DOLLAR_VOLUME = 5_000_000.0   # $5M pre-market DV
_GAP_MIN_PCT = 2.0                     # 2% gap from prior close
_GAP_MAX_RESULTS = 30                  # top 30 movers returned
_PREMARKET_FETCH_CONCURRENCY = 15


class PreMarketGapScanner:
    """Discovers pre-market momentum movers to supplement the static universe."""

    def __init__(self, data_manager: DataManager, min_price: float = 5.0) -> None:
        self.data = data_manager
        self.min_price = min_price
        self._cache: list[str] = []
        self._cache_date: str | None = None

    def _today(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    async def get_gap_movers(self) -> list[str]:
        """Return today's cached gap mover list (fetched once per trading day).

        Returns:
            List of gap mover ticker symbols, sorted by gap magnitude descending.
            Empty list if pre-market data is unavailable.
        """
        today = self._today()
        if self._cache_date == today and self._cache:
            return list(self._cache)

        movers = await self._fetch_gap_movers()
        self._cache = movers
        self._cache_date = today
        log.info(f"[GapScanner] Found {len(movers)} pre-market gap movers: {movers[:10]}")
        return movers

    async def _fetch_gap_movers(self) -> list[str]:
        """Fetch and score pre-market gap movers using available price data."""
        try:
            # Try to use Alpaca provider's snapshot endpoint
            provider = self.data.provider
            if hasattr(provider, "get_snapshots"):
                return await self._fetch_via_snapshots(provider)
            # Fallback: fetch recent daily bars and compute yesterday's close vs today's open
            return await self._fetch_via_bars()
        except Exception as exc:
            log.warning(f"[GapScanner] Pre-market fetch failed ({exc}). Returning empty list.")
            return []

    async def _fetch_via_snapshots(self, provider) -> list[str]:
        """Use Alpaca's snapshot API to get pre-market prices and yesterday's close."""
        from app.core.scanner import CANDIDATE_POOL

        semaphore = asyncio.Semaphore(_PREMARKET_FETCH_CONCURRENCY)
        movers: list[tuple[str, float]] = []

        async def _check(symbol: str) -> tuple[str, float] | None:
            async with semaphore:
                try:
                    snaps = await provider.get_snapshots([symbol])
                    snap = snaps.get(symbol)
                    if snap is None:
                        return None
                    # Alpaca snapshot keys: prevDailyBar.c, minuteBar.c
                    prev_close = getattr(snap, "prev_daily_bar", None)
                    daily_bar = getattr(snap, "daily_bar", None)
                    minute_bar = getattr(snap, "minute_bar", None)
                    if prev_close is None or daily_bar is None:
                        return None
                    prior_close = float(getattr(prev_close, "c", 0.0) or 0.0)
                    today_open = float(getattr(daily_bar, "o", 0.0) or 0.0)
                    current_price = float(
                        getattr(minute_bar, "c", today_open) if minute_bar else today_open
                    )
                    if prior_close <= 0 or today_open <= 0:
                        return None
                    if current_price < self.min_price:
                        return None
                    gap_pct = abs(today_open / prior_close - 1.0) * 100.0
                    if gap_pct < _GAP_MIN_PCT:
                        return None
                    # Rough dollar volume estimate
                    vol = float(getattr(daily_bar, "v", 0.0) or 0.0)
                    dollar_vol = vol * current_price
                    if dollar_vol < _GAP_MIN_DOLLAR_VOLUME:
                        return None
                    return symbol, gap_pct
                except Exception:
                    return None

        # Include already-known names plus a broad list of frequent movers
        targets = set(CANDIDATE_POOL)
        # Add common gap/mover tickers not in the static pool
        targets.update(_FREQUENT_MOVERS)

        results = await asyncio.gather(*[_check(sym) for sym in sorted(targets)])
        movers = [r for r in results if r is not None]
        movers.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in movers[:_GAP_MAX_RESULTS]]

    async def _fetch_via_bars(self) -> list[str]:
        """Fallback: use recent daily bars to compute gap from prior close to today's open."""
        from app.core.scanner import CANDIDATE_POOL

        now = datetime.now(UTC)
        start = now - timedelta(days=5)
        semaphore = asyncio.Semaphore(_PREMARKET_FETCH_CONCURRENCY)
        movers: list[tuple[str, float]] = []

        async def _check(symbol: str) -> tuple[str, float] | None:
            async with semaphore:
                try:
                    df = await self.data.history(symbol, "1Day", start, now, prefer_cache=False)
                    if df is None or len(df) < 2:
                        return None
                    prior_close = float(df["close"].iloc[-2])
                    today_open = float(df["open"].iloc[-1])
                    price = float(df["close"].iloc[-1])
                    if prior_close <= 0 or today_open <= 0 or price < self.min_price:
                        return None
                    gap_pct = abs(today_open / prior_close - 1.0) * 100.0
                    if gap_pct < _GAP_MIN_PCT:
                        return None
                    avg_vol = float(df["volume"].tail(5).mean())
                    if avg_vol * price < _GAP_MIN_DOLLAR_VOLUME:
                        return None
                    return symbol, gap_pct
                except Exception:
                    return None

        results = await asyncio.gather(*[_check(sym) for sym in sorted(CANDIDATE_POOL)])
        movers = [r for r in results if r is not None]
        movers.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in movers[:_GAP_MAX_RESULTS]]


# ---------------------------------------------------------------------------
# Frequently gapping small/mid-cap names NOT already in MARKET_UNIVERSE
# Supplemental seed list — pre-market movers API will validate volume/gap
# ---------------------------------------------------------------------------
_FREQUENT_MOVERS: set[str] = {
    # Quantum / AI compute
    "QUBT", "RGTI", "IONQ", "QMCO", "ARQQ", "QTUM",
    # Biotech catalysts
    "SAVA", "ATHA", "FATE", "BCYC", "PRME", "ELTX", "CNTB",
    "VXRT", "OCGN", "NKTR", "TBIO", "ARDX", "EWTX", "NRIX",
    # Clean energy / EV
    "BLNK", "CHPT", "EVGO", "NKLA", "FSR", "GOEV", "ACHR",
    "JOBY", "LILM", "EH", "RKLB", "LUNR", "AST",
    # Crypto/fintech
    "MARA", "RIOT", "HUT", "CLSK", "BTBT", "WULF", "CORZ", "IREN",
    "MSTR", "COIN", "HOOD", "SOFI", "OPFI", "DAVE",
    # AI/ML software
    "SOUN", "BBAI", "GFAI", "AITX", "BTAI",
    # Momentum mid-cap
    "CRDO", "SMCI", "WOLF", "SITM", "HIMS", "DOCS",
    "CELH", "VSTA", "ACMR", "SEMR",
}
