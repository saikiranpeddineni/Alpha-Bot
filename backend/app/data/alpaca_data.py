"""Alpaca market data provider (alpaca-py SDK).

Uses the free IEX feed by default. Requires ALPACA_API_KEY / ALPACA_API_SECRET.
Provides deep 1-minute history -- the recommended source for serious
backtesting and ML training.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.core.domain import Quote
from app.core.applog import applog
from app.core.universe_discovery import DiscoverableAsset
from app.data.base import DataProvider, empty_bars, normalize_bars

_TF_MAP = {"1Min": ("Minute", 1), "5Min": ("Minute", 5), "15Min": ("Minute", 15), "1Day": ("Day", 1)}

_SNAPSHOT_SUPPRESS_AFTER = 2  # downgrade to DEBUG after this many consecutive failures

# ---------------------------------------------------------------------------
# Global rate limiter — shared across ALL concurrent bar fetches
# Alpaca free tier: ~200 requests/min. We cap at 150/min (25% headroom).
# Token-bucket: replenishes 150 tokens/min (one every 0.4s). Burst up to 20.
# ---------------------------------------------------------------------------
_RATE_LIMIT_PER_MIN: int = 150
_RATE_BURST: int = 20
_RATE_INTERVAL: float = 60.0 / _RATE_LIMIT_PER_MIN  # seconds between tokens


class _TokenBucketRateLimiter:
    """Simple async token-bucket rate limiter. Thread-safe via asyncio.Lock."""

    def __init__(self, rate_per_min: int, burst: int) -> None:
        self._interval = 60.0 / rate_per_min
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = asyncio.get_event_loop().time() if asyncio._get_running_loop() else 0.0  # type: ignore[attr-defined]
        self._lock: asyncio.Lock | None = None  # created lazily after event loop starts

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        lock = self._get_lock()
        async with lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_refill
            self._tokens = min(self._burst, self._tokens + elapsed / self._interval)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Sleep for the time needed to accumulate one token
            wait = (1.0 - self._tokens) * self._interval
        await asyncio.sleep(wait)
        # Re-acquire after sleep (token now available)
        async with self._get_lock():
            self._tokens = max(0.0, self._tokens - 1.0)
            self._last_refill = asyncio.get_event_loop().time()


# Module-level singleton — shared by all AlpacaDataProvider instances
_global_rate_limiter: _TokenBucketRateLimiter | None = None


def _get_rate_limiter() -> _TokenBucketRateLimiter:
    global _global_rate_limiter  # noqa: PLW0603
    if _global_rate_limiter is None:
        _global_rate_limiter = _TokenBucketRateLimiter(_RATE_LIMIT_PER_MIN, _RATE_BURST)
    return _global_rate_limiter


class AlpacaDataProvider(DataProvider):
    name = "alpaca"
    supports_asset_discovery = True

    def __init__(self, api_key: str, api_secret: str) -> None:
        if not (api_key and api_secret):
            raise RuntimeError("Alpaca data provider requires ALPACA_API_KEY and ALPACA_API_SECRET")
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
            from alpaca.data import ScreenerClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("alpaca-py is not installed; run the install script") from exc
        self._client = StockHistoricalDataClient(api_key, api_secret)
        self._trading_client = TradingClient(api_key, api_secret, paper=True)
        try:
            self._screener_client = ScreenerClient(api_key, api_secret)
        except Exception:
            self._screener_client = None
        try:
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64)
            self._client._session.mount("https://", adapter)
            self._client._session.mount("http://", adapter)
        except Exception:
            pass
        self._snapshot_fail_counts: dict[str, int] = {}  # consecutive parse-failure counts per symbol
        self._news_cache: dict[str, tuple[float, datetime]] = {}  # symbol -> (sentiment_score, timestamp)
        
        # Initialize Sentiment Analyzer
        try:
            from app.pis.sentiment import NewsSentimentAnalyzer
            self._sentiment_analyzer = NewsSentimentAnalyzer()
        except ImportError:
            self._sentiment_analyzer = None

    def _fetch_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        unit_name, amount = _TF_MAP[timeframe]
        tf = TimeFrame(amount, TimeFrameUnit.Minute if unit_name == "Minute" else TimeFrameUnit.Day)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        result = self._client.get_stock_bars(req)
        df = result.df
        if df is None or df.empty:
            return empty_bars()
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return normalize_bars(df[["open", "high", "low", "close", "volume"]])

    def _fetch_multi_bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        unit_name, amount = _TF_MAP[timeframe]
        tf = TimeFrame(amount, TimeFrameUnit.Minute if unit_name == "Minute" else TimeFrameUnit.Day)
        # API allows max 10000 symbols, we batch externally if needed
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=tf,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        result = self._client.get_stock_bars(req)
        df = result.df
        out = {}
        if df is None or df.empty:
            return {s: empty_bars() for s in symbols}
        
        # If multiple symbols were returned, Alpaca returns a MultiIndex (symbol, timestamp)
        for s in symbols:
            if isinstance(df.index, pd.MultiIndex):
                if s in df.index.levels[0]:
                    sub_df = df.xs(s, level="symbol")
                    out[s] = normalize_bars(sub_df[["open", "high", "low", "close", "volume"]])
                else:
                    out[s] = empty_bars()
            else:
                # Fallback if only 1 symbol came back in total
                out[s] = normalize_bars(df[["open", "high", "low", "close", "volume"]])
        return out

    async def get_multi_bars(
        self,
        symbols: list[str],
        timeframe: str = "1Min",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        if timeframe not in _TF_MAP:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        end = end or datetime.now(UTC)
        if start is None:
            days = 5 if timeframe != "1Day" else 365
            start = end - timedelta(days=days)

        await _get_rate_limiter().acquire()

        retries = 8
        backoff = 2.0
        for attempt in range(retries):
            try:
                # Alpaca limits multi-bar requests; if symbols list is large we may want to chunk it.
                # Standard scanner is ~30 symbols, which easily fits in a single request (limit 10000).
                dfs = await asyncio.to_thread(self._fetch_multi_bars, [s.upper() for s in symbols], timeframe, start, end)
                if limit:
                    return {s: d.iloc[-limit:] for s, d in dfs.items()}
                return dfs
            except Exception as exc:
                exc_str = str(exc).lower()
                _is_transient = (
                    "too many requests" in exc_str or "429" in exc_str
                    or "getaddrinfo" in exc_str or "nameresolutionerror" in exc_str
                    or "remote end closed" in exc_str or "remotedisconnected" in exc_str
                    or "connection aborted" in exc_str or "connection reset" in exc_str
                    or "timeout" in exc_str or "ssl" in exc_str
                )
                if _is_transient and attempt < retries - 1:
                    await asyncio.sleep(backoff)
                    backoff = min(60.0, backoff * 1.5)
                else:
                    applog.error("data", f"Batched fetch failed after {attempt+1} attempts: {exc}")
                    raise

    async def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Min",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        if timeframe not in _TF_MAP:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        end = end or datetime.now(UTC)
        if start is None:
            days = 5 if timeframe != "1Day" else 365
            start = end - timedelta(days=days)

        # Acquire rate-limit token before making the HTTP request
        await _get_rate_limiter().acquire()

        retries = 8
        backoff = 2.0
        for attempt in range(retries):
            try:
                df = await asyncio.to_thread(self._fetch_bars, symbol.upper(), timeframe, start, end)
                if limit:
                    df = df.iloc[-limit:]
                return df
            except Exception as exc:
                exc_str = str(exc).lower()
                # Transient transport/DNS errors that are worth retrying.
                _is_transient = (
                    "too many requests" in exc_str
                    or "429" in exc_str
                    or "getaddrinfo" in exc_str          # DNS lookup failure
                    or "nameresolutionerror" in exc_str
                    or "remote end closed" in exc_str    # RemoteDisconnected
                    or "remotedisconnected" in exc_str
                    or "connection aborted" in exc_str
                    or "connection reset" in exc_str
                    or "connection refused" in exc_str
                    or "max retries exceeded" in exc_str # urllib3 retry budget
                    or "timed out" in exc_str
                    or "timeout" in exc_str
                    or "ssl" in exc_str                  # transient TLS handshake drops
                )
                if _is_transient and attempt < retries - 1:
                    wait = backoff * (2 ** attempt) + random.uniform(0.1, 1.0)
                    applog.warning(
                        "data.alpaca",
                        f"{symbol}: bars transient error (attempt {attempt + 1}/{retries},"
                        f" retry in {wait:.1f}s): {exc}",
                    )
                    await asyncio.sleep(wait)
                    continue
                applog.error("data.alpaca", f"{symbol}: bars failed: {exc}")
                break
        return empty_bars()

    async def get_news_sentiment(self, symbol: str) -> float:
        """Fetches recent news for a symbol and returns a sentiment score, caching for 5 minutes."""
        if not self._sentiment_analyzer:
            return 0.0
            
        now = datetime.now(UTC)
        if symbol in self._news_cache:
            score, cached_time = self._news_cache[symbol]
            if (now - cached_time).total_seconds() < 300:  # 5 minute cache
                return score
                
        def fetch():
            import requests
            url = f"https://data.alpaca.markets/v1beta1/news?symbols={symbol}&limit=5"
            headers = {
                "APCA-API-KEY-ID": self._client._api_key,
                "APCA-API-SECRET-KEY": self._client._secret_key
            }
            try:
                resp = requests.get(url, headers=headers, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    news = data.get('news', [])
                    return [n['headline'] for n in news]
            except Exception as e:
                applog.error("data.alpaca", f"News fetch failed for {symbol}: {e}")
            return []
            
        headlines = await asyncio.to_thread(fetch)
        score = self._sentiment_analyzer.analyze_headlines(headlines)
        self._news_cache[symbol] = (score, now)
        return score

    async def discover_assets(self) -> tuple[DiscoverableAsset, ...]:
        """Fetch Alpaca's active US-equity asset metadata for bounded discovery.

        First attempts to fetch top actives and movers via ScreenerClient.
        Falls back to standard get_all_assets.
        """
        screener = getattr(self, "_screener_client", None)
        if screener is not None:
            try:
                def fetch_screener() -> list[str]:
                    from alpaca.data import MostActivesRequest, MarketMoversRequest, MarketType
                    from alpaca.data.enums import MostActivesBy
                    symbols = set()

                    # 1. Fetch top 50 most active stocks by volume
                    try:
                        actives = screener.get_most_actives(MostActivesRequest(top=50, by=MostActivesBy.VOLUME))
                        if hasattr(actives, "most_actives") and actives.most_actives:
                            for item in actives.most_actives:
                                if getattr(item, "symbol", None):
                                    symbols.add(item.symbol.upper())
                    except Exception as e:
                        applog.warning("data.alpaca", f"Screener get_most_actives failed: {e}")

                    # 2. Fetch top 50 market movers (gainers)
                    try:
                        movers = screener.get_market_movers(
                            MarketMoversRequest(top=50, market_type=MarketType.STOCKS)
                        )
                        if hasattr(movers, "gainers") and movers.gainers:
                            for item in movers.gainers:
                                if getattr(item, "symbol", None):
                                    symbols.add(item.symbol.upper())
                    except Exception as e:
                        applog.warning("data.alpaca", f"Screener get_market_movers failed: {e}")

                    return list(symbols)

                screener_symbols = await asyncio.to_thread(fetch_screener)
                if screener_symbols:
                    applog.info("data.alpaca", f"Screener discovered {len(screener_symbols)} dynamic assets.")
                    return tuple(
                        DiscoverableAsset(
                            symbol=sym,
                            active=True,
                            tradable=True,
                            asset_class="us_equity",
                        )
                        for sym in sorted(screener_symbols)
                    )
            except Exception as exc:
                applog.warning("data.alpaca", f"Screener discovery failed: {exc}. Falling back to standard asset list.")

        def fetch() -> tuple[DiscoverableAsset, ...]:
            from alpaca.trading.enums import AssetClass, AssetStatus
            from alpaca.trading.requests import GetAssetsRequest

            assets = self._trading_client.get_all_assets(
                GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
            )
            return tuple(
                DiscoverableAsset(
                    symbol=getattr(asset, "symbol", None),
                    active=getattr(asset, "status", None) == AssetStatus.ACTIVE,
                    tradable=bool(getattr(asset, "tradable", False)),
                    asset_class="us_equity" if getattr(asset, "asset_class", None) == AssetClass.US_EQUITY else "",
                    exchange=str(getattr(asset, "exchange", "") or ""),
                )
                for asset in assets
            )

        try:
            return await asyncio.to_thread(fetch)
        except Exception as exc:  # noqa: BLE001
            applog.error("data.alpaca", f"asset discovery failed: {exc}")
            return ()

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        def fetch() -> dict[str, Quote]:
            from alpaca.data.requests import StockSnapshotRequest

            snapshots = self._client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=list(symbols))
            )
            quotes: dict[str, Quote] = {}
            def _valid_float(val: Any) -> float | None:
                if val is None or getattr(type(val), "__module__", "").startswith("unittest.mock"):
                    return None
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None

            for symbol, snap in snapshots.items():
                try:
                    trade = snap.latest_trade
                    daily = snap.daily_bar
                    prev = snap.previous_daily_bar

                    price = None
                    ts = None
                    if trade is not None:
                        price = _valid_float(getattr(trade, "price", None))
                        if price is not None:
                            ts = getattr(trade, "timestamp", None)

                    if price is None and daily is not None:
                        price = _valid_float(getattr(daily, "close", None))
                        if price is not None:
                            ts = getattr(daily, "timestamp", None)

                    if price is None and prev is not None:
                        price = _valid_float(getattr(prev, "close", None))
                        if price is not None:
                            ts = getattr(prev, "timestamp", None)

                    if price is None:
                        raise ValueError("no trade or bar price available in snapshot")

                    prev_close = _valid_float(getattr(prev, "close", None)) if prev is not None else None
                    if prev_close is None:
                        prev_close = price

                    vol = _valid_float(getattr(daily, "volume", None)) if daily is not None else None
                    if vol is None:
                        vol = 0.0

                    ts_dt = ts if ts is not None and getattr(ts, "tzinfo", None) and not _valid_float(getattr(type(ts), "__module__", None)) else utcnow()

                    quotes[symbol.upper()] = Quote(
                        symbol=symbol.upper(),
                        price=round(price, 2),
                        change_pct=round((price / prev_close - 1.0) * 100.0, 3) if prev_close else 0.0,
                        volume=vol,
                        ts=ts_dt,
                    )
                    self._snapshot_fail_counts.pop(symbol, None)  # reset on success
                except Exception as exc:  # noqa: BLE001
                    count = self._snapshot_fail_counts.get(symbol, 0) + 1
                    self._snapshot_fail_counts[symbol] = count
                    if count <= _SNAPSHOT_SUPPRESS_AFTER:
                        applog.warning("data.alpaca", f"{symbol}: snapshot parse failed: {exc}")
                    else:
                        applog.debug(
                            "data.alpaca",
                            f"{symbol}: snapshot parse suppressed (failed {count}x): {exc}",
                        )
            return quotes

        try:
            return await asyncio.to_thread(fetch)
        except Exception as exc:  # noqa: BLE001
            applog.error("data.alpaca", f"snapshot batch failed: {exc}")
            return {}

    def get_snapshot_fail_summary(self) -> dict[str, int]:
        """Return symbols with suppressed snapshot parse failures and their consecutive fail counts.

        Symbols appear here when they have failed more than _SNAPSHOT_SUPPRESS_AFTER times
        consecutively. Useful for identifying permanently dead symbols to blocklist.
        """
        return {s: c for s, c in self._snapshot_fail_counts.items() if c > _SNAPSHOT_SUPPRESS_AFTER}
