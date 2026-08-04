"""Top Gainers & Losers Scanner.

Queries Alpaca's screener endpoint (or falls back to computed daily-bar change%)
to identify the day's strongest momentum stocks — both directionally:

  gainers → ENTER_LONG candidates (momentum continuation)
  losers  → ENTER_SHORT candidates (breakdown continuation)

Cached once per trading session. Fail-open: returns empty lists if data
is unavailable so the rest of the scanner pipeline is never blocked.

Alpaca endpoint used (when available):
  GET /v1beta1/screener/stocks/movers?by=change_percent&top=20
  Response: {"gainers": [...], "losers": [...]}

Fallback (when screener endpoint unavailable):
  Fetch last 2 daily bars for CANDIDATE_POOL and compute today_change_pct
  from prior close → today open.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.data.manager import DataManager

log = logging.getLogger(__name__)

# How many top gainers / losers to return per session
_TOP_N = 20

# Minimum price and dollar volume for a mover to be considered
_MIN_PRICE = 5.0
_MIN_DOLLAR_VOLUME = 2_000_000.0   # $2M daily dollar volume

# Minimum directional move to qualify
_MIN_GAINER_PCT = 2.5    # up ≥ 2.5% today
_MIN_LOSER_PCT  = -2.5   # down ≤ -2.5% today

# Concurrency for bar-based fallback
_FALLBACK_CONCURRENCY = 20


@dataclass
class MoverEntry:
    symbol: str
    direction: str          # "gainer" or "loser"
    change_pct: float       # today's % change (positive for gainers, negative for losers)
    price: float = 0.0
    dollar_volume: float = 0.0


@dataclass
class TopMoversResult:
    gainers: list[str] = field(default_factory=list)
    losers: list[str] = field(default_factory=list)
    entries: dict[str, MoverEntry] = field(default_factory=dict)  # symbol → MoverEntry
    source: str = "unknown"   # "screener" | "bars" | "empty"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class TopMoversScanner:
    """Discovers top daily gainers and losers for long/short entry candidacy."""

    def __init__(self, data_manager: DataManager, min_price: float = _MIN_PRICE) -> None:
        self.data = data_manager
        self.min_price = min_price
        self._cache: TopMoversResult | None = None
        self._cache_date: str | None = None

    def _today(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    async def get_movers(self) -> TopMoversResult:
        """Return today's cached top gainers and losers.

        Returns cached result if already fetched today. Fetches fresh otherwise.
        Always returns a valid TopMoversResult (never raises).
        """
        today = self._today()
        if self._cache_date == today and self._cache is not None:
            return self._cache

        result = await self._fetch_movers()
        self._cache = result
        self._cache_date = today
        log.info(
            f"[TopMovers] {len(result.gainers)} gainers | {len(result.losers)} losers "
            f"(source={result.source}) | "
            f"gainers={result.gainers[:5]} losers={result.losers[:5]}"
        )
        return result

    async def _fetch_movers(self) -> TopMoversResult:
        """Try screener API first, fall back to bar-computed change%."""
        try:
            result = await self._fetch_via_screener()
            if result.gainers or result.losers:
                return result
        except Exception as exc:
            log.debug(f"[TopMovers] Screener API unavailable ({exc}), using bar fallback")

        try:
            return await self._fetch_via_bars()
        except Exception as exc:
            log.warning(f"[TopMovers] Bar fallback also failed ({exc}). Returning empty.")
            return TopMoversResult(source="empty")

    async def _fetch_via_screener(self) -> TopMoversResult:
        """Use Alpaca's /v1beta1/screener/stocks/movers endpoint."""
        provider = self.data.provider
        client = getattr(provider, "_client", None) or getattr(provider, "client", None)
        if client is None:
            raise RuntimeError("No Alpaca client available on provider")

        # Try both alpaca-py and raw REST approaches
        gainers_raw: list[dict] = []
        losers_raw: list[dict] = []

        # alpaca-py StockScreenerRequest
        try:
            from alpaca.data.requests import ScreenerRequest
            from alpaca.data.historical import StockHistoricalDataClient
            if isinstance(client, StockHistoricalDataClient):
                req = ScreenerRequest(by="change_percent", top=_TOP_N)
                resp = client.get_stock_most_actives(req)
                # screener returns MostActives, try movers differently
        except Exception:
            pass

        # Direct REST fallback using alpaca-py trading client
        try:
            trading_client = (
                getattr(provider, "_trading_client", None)
                or getattr(provider, "trading_client", None)
            )
            if trading_client and hasattr(trading_client, "get_stock_movers"):
                movers = trading_client.get_stock_movers(by="change_percent", top=_TOP_N)
                gainers_raw = [m.__dict__ for m in getattr(movers, "gainers", [])]
                losers_raw = [m.__dict__ for m in getattr(movers, "losers", [])]
        except Exception:
            pass

        # Raw HTTP via requests session on alpaca client
        if not gainers_raw and not losers_raw:
            session = getattr(client, "_session", None) or getattr(client, "session", None)
            if session and hasattr(session, "get"):
                url = "https://data.alpaca.markets/v1beta1/screener/stocks/movers"
                params = {"by": "change_percent", "top": str(_TOP_N)}
                resp = await asyncio.to_thread(session.get, url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    gainers_raw = data.get("gainers", [])
                    losers_raw = data.get("losers", [])

        if not gainers_raw and not losers_raw:
            raise RuntimeError("Screener returned empty data")

        entries: dict[str, MoverEntry] = {}
        gainers: list[str] = []
        losers: list[str] = []

        for raw in gainers_raw:
            sym = str(raw.get("symbol", "") or raw.get("Symbol", "")).upper()
            price = float(raw.get("price", 0.0) or raw.get("close", 0.0) or 0.0)
            change = float(raw.get("percent_change", 0.0) or raw.get("change_percent", 0.0) or 0.0)
            if sym and price >= self.min_price and change >= _MIN_GAINER_PCT:
                entries[sym] = MoverEntry(sym, "gainer", change, price)
                gainers.append(sym)

        for raw in losers_raw:
            sym = str(raw.get("symbol", "") or raw.get("Symbol", "")).upper()
            price = float(raw.get("price", 0.0) or raw.get("close", 0.0) or 0.0)
            change = float(raw.get("percent_change", 0.0) or raw.get("change_percent", 0.0) or 0.0)
            if sym and price >= self.min_price and change <= _MIN_LOSER_PCT:
                entries[sym] = MoverEntry(sym, "loser", change, price)
                losers.append(sym)

        return TopMoversResult(gainers=gainers, losers=losers, entries=entries, source="screener")

    async def _fetch_via_bars(self) -> TopMoversResult:
        """Compute today's change% from daily bars for the static CANDIDATE_POOL."""
        from app.core.scanner import CANDIDATE_POOL

        now = datetime.now(UTC)
        start = now - timedelta(days=5)
        semaphore = asyncio.Semaphore(_FALLBACK_CONCURRENCY)
        mover_entries: list[MoverEntry] = []

        async def _check(symbol: str) -> MoverEntry | None:
            async with semaphore:
                try:
                    df = await self.data.history(symbol, "1Day", start, now, prefer_cache=False)
                    if df is None or len(df) < 2:
                        return None
                    prior_close = float(df["close"].iloc[-2])
                    today_open  = float(df["open"].iloc[-1])
                    today_close = float(df["close"].iloc[-1])
                    price = today_close
                    if prior_close <= 0 or price < self.min_price:
                        return None
                    change_pct = (today_close - prior_close) / prior_close * 100.0
                    avg_vol = float(df["volume"].tail(3).mean())
                    dv = avg_vol * price
                    if dv < _MIN_DOLLAR_VOLUME:
                        return None
                    if change_pct >= _MIN_GAINER_PCT:
                        return MoverEntry(symbol, "gainer", change_pct, price, dv)
                    if change_pct <= _MIN_LOSER_PCT:
                        return MoverEntry(symbol, "loser", change_pct, price, dv)
                    return None
                except Exception:
                    return None

        results = await asyncio.gather(*[_check(sym) for sym in sorted(CANDIDATE_POOL)])
        mover_entries = [r for r in results if r is not None]

        gainers = sorted(
            [e.symbol for e in mover_entries if e.direction == "gainer"],
            key=lambda s: next(e.change_pct for e in mover_entries if e.symbol == s),
            reverse=True,
        )[:_TOP_N]

        losers = sorted(
            [e.symbol for e in mover_entries if e.direction == "loser"],
            key=lambda s: next(e.change_pct for e in mover_entries if e.symbol == s),
        )[:_TOP_N]

        entries = {e.symbol: e for e in mover_entries if e.symbol in set(gainers) | set(losers)}
        return TopMoversResult(gainers=gainers, losers=losers, entries=entries, source="bars")
