"""Yahoo Finance data provider (via yfinance).

Free, keyless real market data. Intraday history is limited by Yahoo
(1m bars: ~7 days back; 5m bars: ~60 days) and delayed -- good for paper
trading and short backtests without any account setup.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.core.domain import Quote
from app.core.applog import applog
from app.data.base import DataProvider, empty_bars, normalize_bars, resample_bars

_INTERVALS = {"1Min": "1m", "5Min": "5m", "15Min": "15m", "1Day": "1d"}
_MAX_LOOKBACK = {"1Min": timedelta(days=7), "5Min": timedelta(days=59), "15Min": timedelta(days=59)}


class YahooProvider(DataProvider):
    name = "yahoo"

    def __init__(self) -> None:
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("yfinance is not installed; run the install script") from exc

    def _download(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        import yfinance as yf

        df = yf.download(
            tickers=symbol,
            interval=interval,
            start=start,
            end=end,
            auto_adjust=True,
            prepost=False,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            return empty_bars()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return normalize_bars(df)

    async def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Min",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        interval = _INTERVALS.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        end = end or datetime.now(UTC)
        if start is None:
            start = end - timedelta(days=5 if timeframe != "1Day" else 365)
        max_back = _MAX_LOOKBACK.get(timeframe)
        if max_back and end - start > max_back:
            clamped = end - max_back
            applog.warning(
                "data.yahoo",
                f"{symbol}: Yahoo limits {timeframe} history; clamping start "
                f"{start.date()} -> {clamped.date()}",
            )
            start = clamped
        try:
            df = await asyncio.to_thread(self._download, symbol.upper(), interval, start, end)
        except Exception as exc:  # noqa: BLE001
            applog.error("data.yahoo", f"{symbol}: download failed: {exc}")
            return empty_bars()
        df = resample_bars(df, timeframe) if timeframe not in _INTERVALS else df
        if limit:
            df = df.iloc[-limit:]
        return df

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}

        def fetch() -> dict[str, Quote]:
            import yfinance as yf

            quotes: dict[str, Quote] = {}
            for symbol in symbols:
                try:
                    info = yf.Ticker(symbol).fast_info
                    price = float(info["last_price"])
                    prev = float(info.get("previous_close") or price)
                    quotes[symbol.upper()] = Quote(
                        symbol=symbol.upper(),
                        price=round(price, 2),
                        change_pct=round((price / prev - 1.0) * 100.0, 3) if prev else 0.0,
                        volume=float(info.get("last_volume") or 0.0),
                        ts=datetime.now(UTC),
                    )
                except Exception as exc:  # noqa: BLE001
                    applog.warning("data.yahoo", f"{symbol}: quote failed: {exc}")
            return quotes

        try:
            out = await asyncio.to_thread(fetch)
        except Exception as exc:  # noqa: BLE001
            applog.error("data.yahoo", f"quote batch failed: {exc}")
        return out
