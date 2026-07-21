"""DataManager -- the engine's single window onto market data.

Wraps the active provider with rolling in-memory frames, closed-bar
detection, timeframe resampling, quote fallbacks and the SQLite history
cache used by backtests and ML training.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.core.domain import Quote
from app.core.logging import applog
from app.data.base import DataProvider, empty_bars, resample_bars, timeframe_delta, HistoricalDataCoverageError, CachePolicy
from app.data.store import BarCache
from app.data.synthetic import SyntheticProvider
from app.data.session_coverage import validate_requested_session_coverage

CLOSED_BAR_GRACE_SEC = 3.0


def make_provider(name: str, settings) -> DataProvider:  # noqa: ANN001
    """Data provider factory. Register new providers here."""
    if name == "synthetic":
        return SyntheticProvider()
    if name == "yahoo":
        from app.data.yahoo import YahooProvider

        return YahooProvider()
    if name == "alpaca":
        from app.data.alpaca_data import AlpacaDataProvider

        return AlpacaDataProvider(settings.alpaca_api_key, settings.alpaca_api_secret)
    raise ValueError(f"Unknown data provider {name!r}")


class DataManager:
    def __init__(self, provider: DataProvider, cache_path: Path, max_rows: int = 3000) -> None:
        self.provider = provider
        self.cache = BarCache(cache_path)
        self.max_rows = max_rows
        self._frames: dict[str, pd.DataFrame] = {}
        self._last_closed_ts: dict[str, pd.Timestamp] = {}
        self.provenance: dict[str, dict] = {}
        self.cache_policy: str = "default"

    @property
    def is_synthetic(self) -> bool:
        return isinstance(self.provider, SyntheticProvider)

    # ---- live rolling frames ----

    async def warmup(self, symbols: list[str], lookback_bars: int = 800) -> None:
        extended = list(symbols)
        if not self.is_synthetic:
            for v in ["VIX", "^VIX"]:
                if v not in extended:
                    extended.append(v)
        for symbol in extended:
            symbol = symbol.upper()
            try:
                provider = self.provider
                if symbol in ("VIX", "^VIX") and not self.is_synthetic:
                    from app.data.yahoo import YahooProvider
                    provider = YahooProvider()
                df = await provider.get_bars(symbol, "1Min", limit=lookback_bars)
            except Exception as exc:  # noqa: BLE001
                applog.error("data", f"{symbol}: warmup failed: {exc}")
                df = empty_bars()
            self._frames[symbol] = df
            closed = self._closed_slice(df)
            if not closed.empty:
                self._last_closed_ts[symbol] = closed.index[-1]
            applog.info("data", f"{symbol}: warmed up with {len(df)} bars ({provider.name})")

    def _closed_slice(self, df: pd.DataFrame) -> pd.DataFrame:
        """Bars whose 1-minute window has fully elapsed (synthetic bars are always closed)."""
        if df.empty or self.is_synthetic:
            return df
        cutoff = pd.Timestamp(datetime.now(UTC) - timedelta(seconds=60 + CLOSED_BAR_GRACE_SEC))
        return df[df.index <= cutoff]

    async def poll(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Fetch newest bars; return {symbol: frame_of_newly_closed_bars}."""
        extended = list(symbols)
        if not self.is_synthetic:
            for v in ["VIX", "^VIX"]:
                if v not in extended:
                    extended.append(v)

        if self.is_synthetic:
            for symbol in extended:
                self.provider._ensure(symbol)  # type: ignore[attr-defined]
            self.provider.advance()  # type: ignore[attr-defined]

        new_closed: dict[str, pd.DataFrame] = {}
        for symbol in extended:
            symbol = symbol.upper()
            try:
                provider = self.provider
                if symbol in ("VIX", "^VIX") and not self.is_synthetic:
                    from app.data.yahoo import YahooProvider
                    provider = YahooProvider()
                tail = await provider.get_bars(symbol, "1Min", limit=30)
            except Exception as exc:  # noqa: BLE001
                applog.warning("data", f"{symbol}: poll failed: {exc}")
                continue
            if tail.empty:
                continue
            current = self._frames.get(symbol, empty_bars())
            merged = pd.concat([current, tail])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            if len(merged) > self.max_rows:
                merged = merged.iloc[-self.max_rows :]
            self._frames[symbol] = merged

            closed = self._closed_slice(merged)
            if closed.empty:
                continue
            last_seen = self._last_closed_ts.get(symbol)
            fresh = closed if last_seen is None else closed[closed.index > last_seen]
            if not fresh.empty:
                self._last_closed_ts[symbol] = closed.index[-1]
                new_closed[symbol] = fresh
        return new_closed

    def frame(self, symbol: str, timeframe: str = "1Min", limit: int | None = None) -> pd.DataFrame:
        df = self._frames.get(symbol.upper(), empty_bars())
        if timeframe != "1Min":
            df = resample_bars(df, timeframe)
        if limit:
            df = df.iloc[-limit:]
        return df

    def last_price(self, symbol: str) -> float | None:
        df = self._frames.get(symbol.upper())
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for symbol in symbols:
            price = self.last_price(symbol)
            if price is not None:
                out[symbol.upper()] = price
        return out

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        try:
            quotes = await self.provider.get_quotes(symbols)
        except Exception as exc:  # noqa: BLE001
            applog.warning("data", f"quotes failed: {exc}")
            quotes = {}
        # Fall back to the rolling frame for anything the provider missed
        for symbol in symbols:
            symbol = symbol.upper()
            if symbol not in quotes:
                price = self.last_price(symbol)
                if price is not None:
                    quotes[symbol] = Quote(symbol=symbol, price=round(price, 2))
        return quotes

    def validate_cache_coverage(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        cached_df: pd.DataFrame,
    ) -> None:
        """Fail closed when requested immutable history is materially incomplete."""
        validate_requested_session_coverage(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            cached_df=cached_df,
        )

    def _record_provenance(
        self,
        symbol: str,
        source: str,
        provider_name: str | None,
        df: pd.DataFrame,
    ) -> None:
        if df.empty:
            self.provenance[symbol] = {
                "symbol": symbol,
                "source": source,
                "provider": provider_name,
                "bar_count": 0,
                "first_bar": None,
                "last_bar": None,
                "regular_session_count": 0,
            }
            return

        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        et_idx = df.index.tz_convert(ET)
        minutes = et_idx.hour * 60 + et_idx.minute
        reg_session_dates = set(et_idx[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)].date)

        self.provenance[symbol] = {
            "symbol": symbol,
            "source": source,
            "provider": provider_name,
            "bar_count": len(df),
            "first_bar": df.index[0].to_pydatetime(),
            "last_bar": df.index[-1].to_pydatetime(),
            "regular_session_count": len(reg_session_dates),
        }

    # ---- deep history (backtests / ML training) ----

    async def history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        prefer_cache: bool = True,
        cache_policy: str | None = None,
    ) -> pd.DataFrame:
        """Range fetch with cache-first reads and automatic cache writes.

        Synthetic data is regenerated deterministically instead of cached.
        """
        symbol = symbol.upper()
        policy = cache_policy or self.cache_policy
        if self.is_synthetic:
            df = await self.provider.get_bars(symbol, timeframe, start=start, end=end)
            self._record_provenance(symbol, "network", self.provider.name, df)
            return df

        if policy == CachePolicy.IMMUTABLE_CACHE_ONLY:
            cached = self.cache.query(symbol, timeframe, start, end)
            self.validate_cache_coverage(symbol, timeframe, start, end, cached)
            self._record_provenance(symbol, "cache", None, cached)
            return cached

        # Non-immutable (default) policy
        if prefer_cache:
            cached = self.cache.query(symbol, timeframe, start, end)
            if not cached.empty:
                try:
                    self.validate_cache_coverage(symbol, timeframe, start, end, cached)
                    span_ok = True
                except HistoricalDataCoverageError:
                    span_ok = False
                if span_ok:
                    self._record_provenance(symbol, "cache", None, cached)
                    return cached

        # Cache miss or coverage failure -> live network fetch
        provider = self.provider
        if symbol in ("VIX", "^VIX") and not self.is_synthetic:
            from app.data.yahoo import YahooProvider
            provider = YahooProvider()
        df = await provider.get_bars(symbol, timeframe, start=start, end=end)
        source = "network"
        provider_name = provider.name
        if df.empty:
            cached = self.cache.query(symbol, timeframe, start, end)
            if not cached.empty:
                applog.warning("data", f"{symbol}: provider empty; serving {len(cached)} cached bars")
                df = cached
                source = "cache"
                provider_name = None
        else:
            self.cache.upsert(symbol, timeframe, df)

        self._record_provenance(symbol, source, provider_name, df)
        return df

    async def close(self) -> None:
        await self.provider.close()
        self.cache.close()
