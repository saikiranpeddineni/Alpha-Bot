"""Dynamic Market Opportunity Scanner.

Combines three live data sources to feed MarketScanner Stage 1 with
the highest-quality intraday candidates:

1. Pre-Market Gap Movers (9:00 AM ET):
   Stocks gapping 2%+ from prior close on pre-market volume ≥ $5M.
   Sourced from Alpaca snapshot endpoint or daily bar computation.

2. Top Daily Gainers (9:30 AM ET onwards):
   Stocks up ≥ 2.5% on the day — ENTER_LONG momentum candidates.
   Sourced from Alpaca screener /v1beta1/screener/stocks/movers.

3. Top Daily Losers (9:30 AM ET onwards):
   Stocks down ≤ -2.5% on the day — ENTER_SHORT breakdown candidates.
   Sourced from Alpaca screener /v1beta1/screener/stocks/movers.

All three are injected into MarketScanner Stage 1 alongside the
static MARKET_UNIVERSE (278+ symbols), giving the scanner a combined
pool of 350+ live, actively moving candidates per scan cycle.

Fail-open: any individual source failure returns empty and falls back
to the other sources without blocking the scanner pipeline.
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
_PREMARKET_FETCH_CONCURRENCY = 5      # was 10 — reduced to share Alpaca rate limit budget


class PreMarketGapScanner:
    """Discovers pre-market gap movers + intraday top gainers/losers."""

    def __init__(self, data_manager: DataManager, min_price: float = 5.0) -> None:
        self.data = data_manager
        self.min_price = min_price
        self._cache: list[str] = []
        self._cache_date: str | None = None
        # Mover context: symbol -> {"direction": "gainer"|"loser", "change_pct": float}
        self.mover_context: dict[str, dict] = {}
        # Lazy-init TopMoversScanner to avoid circular import at module load
        self._top_movers_scanner = None

    def _today(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _get_top_movers_scanner(self):
        if self._top_movers_scanner is None:
            from app.core.top_movers_scanner import TopMoversScanner
            self._top_movers_scanner = TopMoversScanner(self.data, min_price=self.min_price)
        return self._top_movers_scanner

    async def get_gap_movers(self) -> list[str]:
        """Return today's combined mover list from all three sources.

        Sources combined:
        - Pre-market gap movers (snapshots or bar-computed)
        - Top daily gainers (ENTER_LONG candidates)
        - Top daily losers  (ENTER_SHORT candidates)

        Returns:
            Sorted list of all unique symbols from all three sources.
            Empty list if all sources are unavailable.
        """
        today = self._today()
        if self._cache_date == today and self._cache:
            return list(self._cache)

        # Fetch all three sources concurrently
        gap_task = asyncio.create_task(self._fetch_gap_movers())
        movers_task = asyncio.create_task(self._fetch_top_movers())
        gap_movers, top_movers_result = await asyncio.gather(gap_task, movers_task)

        # Build combined deduped symbol list
        all_symbols: set[str] = set(gap_movers)
        all_symbols.update(top_movers_result.gainers)
        all_symbols.update(top_movers_result.losers)

        # Build/update mover context for direction tagging
        new_context: dict[str, dict] = {}
        for sym in gap_movers:
            new_context[sym] = new_context.get(sym, {"source": "gap", "direction": "gainer"})
        for sym in top_movers_result.gainers:
            entry = top_movers_result.entries.get(sym)
            new_context[sym] = {
                "source": "top_gainer",
                "direction": "gainer",
                "change_pct": entry.change_pct if entry else 0.0,
            }
        for sym in top_movers_result.losers:
            entry = top_movers_result.entries.get(sym)
            new_context[sym] = {
                "source": "top_loser",
                "direction": "loser",
                "change_pct": entry.change_pct if entry else 0.0,
            }
        self.mover_context = new_context

        combined = sorted(all_symbols)
        self._cache = combined
        self._cache_date = today
        log.info(
            f"[GapScanner] Combined movers: {len(gap_movers)} gap | "
            f"{len(top_movers_result.gainers)} gainers | "
            f"{len(top_movers_result.losers)} losers → {len(combined)} total"
        )
        return combined

    async def _fetch_top_movers(self):
        """Fetch top gainers/losers via TopMoversScanner. Fail-open."""
        try:
            return await self._get_top_movers_scanner().get_movers()
        except Exception as exc:
            log.warning(f"[GapScanner] TopMovers fetch failed ({exc})")
            from app.core.top_movers_scanner import TopMoversResult
            return TopMoversResult(source="empty")

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

        # Include already-known names plus a broad list of frequent movers
        targets = set(CANDIDATE_POOL)
        targets.update(_FREQUENT_MOVERS)
        target_list = sorted(targets)
        
        try:
            snaps = await provider.get_snapshots(target_list)
        except Exception as exc:
            log.warning(f"[GapScanner] get_snapshots batch failed ({exc})")
            return []
            
        for symbol, snap in snaps.items():
            try:
                if snap is None:
                    continue
                # Alpaca snapshot keys: prevDailyBar.c, minuteBar.c
                prev_close = getattr(snap, "prev_daily_bar", None)
                daily_bar = getattr(snap, "daily_bar", None)
                minute_bar = getattr(snap, "minute_bar", None)
                if prev_close is None or daily_bar is None:
                    continue
                prior_close = float(getattr(prev_close, "c", 0.0) or 0.0)
                today_open = float(getattr(daily_bar, "o", 0.0) or 0.0)
                current_price = float(
                    getattr(minute_bar, "c", today_open) if minute_bar else today_open
                )
                if prior_close <= 0 or today_open <= 0:
                    continue
                if current_price < self.min_price:
                    continue
                gap_pct = abs(today_open / prior_close - 1.0) * 100.0
                if gap_pct < _GAP_MIN_PCT:
                    continue
                # Rough dollar volume estimate
                vol = float(getattr(daily_bar, "v", 0.0) or 0.0)
                dollar_vol = vol * current_price
                if dollar_vol < _GAP_MIN_DOLLAR_VOLUME:
                    continue
                movers.append((symbol, gap_pct))
            except Exception:
                continue

        movers.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in movers[:_GAP_MAX_RESULTS]]

    async def _fetch_via_bars(self) -> list[str]:
        """Fallback: use recent daily bars to compute gap from prior close to today's open."""
        from app.core.scanner import CANDIDATE_POOL

        now = datetime.now(UTC)
        start = now - timedelta(days=5)
        
        res_1d = {}
        if hasattr(self.data, "multi_history"):
            try:
                batched = await self.data.multi_history(CANDIDATE_POOL, "1Day", start, now, prefer_cache=False)
                if isinstance(batched, dict):
                    res_1d = batched
            except Exception as exc:
                log.warning(f"[GapScanner] multi_history batch failed ({exc})")

        semaphore = asyncio.Semaphore(_PREMARKET_FETCH_CONCURRENCY)
        movers: list[tuple[str, float]] = []

        async def _check(symbol: str) -> tuple[str, float] | None:
            async with semaphore:
                try:
                    if symbol in res_1d:
                        df = res_1d[symbol]
                    else:
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
