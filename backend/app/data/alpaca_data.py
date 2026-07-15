"""Alpaca market data provider (alpaca-py SDK).

Uses the free IEX feed by default. Requires ALPACA_API_KEY / ALPACA_API_SECRET.
Provides deep 1-minute history -- the recommended source for serious
backtesting and ML training.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.core.domain import Quote
from app.core.logging import applog
from app.core.universe_discovery import DiscoverableAsset
from app.data.base import DataProvider, empty_bars, normalize_bars

_TF_MAP = {"1Min": ("Minute", 1), "5Min": ("Minute", 5), "15Min": ("Minute", 15), "1Day": ("Day", 1)}

_SNAPSHOT_SUPPRESS_AFTER = 2  # downgrade to DEBUG after this many consecutive failures


class AlpacaDataProvider(DataProvider):
    name = "alpaca"
    supports_asset_discovery = True

    def __init__(self, api_key: str, api_secret: str) -> None:
        if not (api_key and api_secret):
            raise RuntimeError("Alpaca data provider requires ALPACA_API_KEY and ALPACA_API_SECRET")
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("alpaca-py is not installed; run the install script") from exc
        self._client = StockHistoricalDataClient(api_key, api_secret)
        self._trading_client = TradingClient(api_key, api_secret, paper=True)
        try:
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64)
            self._client._session.mount("https://", adapter)
            self._client._session.mount("http://", adapter)
        except Exception:
            pass
        self._snapshot_fail_counts: dict[str, int] = {}  # consecutive parse-failure counts per symbol

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

        retries = 5
        backoff = 0.5
        for attempt in range(retries):
            try:
                df = await asyncio.to_thread(self._fetch_bars, symbol.upper(), timeframe, start, end)
                if limit:
                    df = df.iloc[-limit:]
                return df
            except Exception as exc:
                exc_str = str(exc).lower()
                if "too many requests" in exc_str or "429" in exc_str:
                    if attempt < retries - 1:
                        await asyncio.sleep(backoff * (2 ** attempt))
                        continue
                applog.error("data.alpaca", f"{symbol}: bars failed: {exc}")
                break
        return empty_bars()

    async def discover_assets(self) -> tuple[DiscoverableAsset, ...]:
        """Fetch Alpaca's active US-equity asset metadata for bounded discovery."""
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
            for symbol, snap in snapshots.items():
                try:
                    trade = snap.latest_trade
                    daily = snap.daily_bar
                    prev = snap.previous_daily_bar
                    price = float(trade.price)
                    prev_close = float(prev.close) if prev else price
                    quotes[symbol.upper()] = Quote(
                        symbol=symbol.upper(),
                        price=round(price, 2),
                        change_pct=round((price / prev_close - 1.0) * 100.0, 3) if prev_close else 0.0,
                        volume=float(daily.volume) if daily else 0.0,
                        ts=trade.timestamp if trade.timestamp.tzinfo else trade.timestamp.replace(tzinfo=UTC),
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
