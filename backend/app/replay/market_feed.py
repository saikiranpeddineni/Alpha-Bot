"""Deterministic historical market-data adapter for production replay.

ReplayDataManager deliberately implements the subset of DataManager consumed by
TradingEngine.  It exposes only bars whose timestamps are <= the simulated
clock, so the live engine cannot look ahead during replay.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from app.config import DATA_DIR
from app.core.domain import Quote
from app.core.logging import applog
from app.data.base import empty_bars, resample_bars


class ReplayMarketFeed:
    """Load immutable 1-minute historical bars in deterministic order."""

    def __init__(self, data_path: Path | None = None) -> None:
        self.data_path = data_path or (DATA_DIR / "replay_bars.jsonl")
        self.symbols: set[str] = set()

    def load_bars(self, start_date: str, end_date: str, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.data_path.exists():
            alt_path = Path(__file__).resolve().parents[3] / "scratch" / "july24_replay_stream.jsonl"
            if alt_path.exists():
                self.data_path = alt_path
        if not self.data_path.exists():
            raise FileNotFoundError(f"Replay market data not found: {self.data_path}")

        wanted = {s.upper() for s in symbols} if symbols else None
        with self.data_path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception as exc:
                    raise ValueError(f"Invalid replay JSONL at line {line_no}: {exc}") from exc
                if row.get("kind") != "bar" and "open" not in row:
                    continue
                symbol = str(row.get("symbol", "")).upper()
                if not symbol or (wanted and symbol not in wanted):
                    continue
                raw_ts = row.get("ts") or row.get("t")
                if not raw_ts:
                    raise ValueError(f"Replay bar missing timestamp at line {line_no}")
                dt = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else raw_ts
                if not isinstance(dt, datetime):
                    raise ValueError(f"Unsupported replay timestamp at line {line_no}: {raw_ts!r}")
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                day = dt.strftime("%Y-%m-%d")
                if day < start_date or day > end_date:
                    continue
                normalized = dict(row)
                normalized["ts_dt"] = dt
                normalized["symbol"] = symbol
                rows.append(normalized)
                self.symbols.add(symbol)

        rows.sort(key=lambda r: (r["ts_dt"], r["symbol"]))
        if not rows:
            raise ValueError(f"No replay bars for {start_date}..{end_date} symbols={symbols or 'ALL'}")
        return rows


class ReplayDataManager:
    """As-of data window implementing TradingEngine's production DataManager contract."""

    def __init__(self, bars: list[dict[str, Any]], max_rows: int = 3000) -> None:
        if not bars:
            raise ValueError("ReplayDataManager requires at least one historical bar")
        self.provider = SimpleNamespace(name="replay")
        self.max_rows = max_rows
        self.cache_policy = "immutable_replay"
        self.provenance: dict[str, dict[str, Any]] = {}
        self._frames: dict[str, pd.DataFrame] = {}
        self._pending_by_ts: dict[datetime, dict[str, list[dict[str, Any]]]] = {}
        self._current_ts: datetime | None = None
        self._delivered_ts: set[datetime] = set()

        for bar in bars:
            ts = bar["ts_dt"]
            symbol = bar["symbol"].upper()
            self._pending_by_ts.setdefault(ts, {}).setdefault(symbol, []).append(bar)

    @property
    def is_synthetic(self) -> bool:
        return False

    async def warmup(self, symbols: list[str], lookback_bars: int = 800) -> None:
        # Replay begins from the first supplied historical event. Any required
        # warmup bars must therefore be included in the immutable dataset.
        return None

    def set_time(self, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._current_ts = ts

    async def poll(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        if self._current_ts is None or self._current_ts in self._delivered_ts:
            return {}
        self._delivered_ts.add(self._current_ts)
        batch = self._pending_by_ts.get(self._current_ts, {})
        requested = {s.upper() for s in symbols}
        out: dict[str, pd.DataFrame] = {}

        for symbol, rows in sorted(batch.items()):
            # The live DataManager only polls the engine watchlist (+ VIX). Do
            # the same here; scanner expansion changes the watchlist naturally.
            if symbol not in requested and symbol not in {"VIX", "^VIX"}:
                continue
            records = []
            indexes = []
            for row in rows:
                records.append({
                    "open": float(row.get("open", row.get("o", row.get("close", row.get("c"))))),
                    "high": float(row.get("high", row.get("h", row.get("close", row.get("c"))))),
                    "low": float(row.get("low", row.get("l", row.get("close", row.get("c"))))),
                    "close": float(row.get("close", row.get("c"))),
                    "volume": float(row.get("volume", row.get("v", 0.0))),
                })
                indexes.append(pd.Timestamp(row["ts_dt"]))
            fresh = pd.DataFrame(records, index=pd.DatetimeIndex(indexes, name="timestamp")).sort_index()
            current = self._frames.get(symbol, empty_bars())
            merged = pd.concat([current, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index().iloc[-self.max_rows:]
            self._frames[symbol] = merged
            out[symbol] = fresh
        return out

    def frame(self, symbol: str, timeframe: str = "1Min", limit: int | None = None) -> pd.DataFrame:
        df = self._frames.get(symbol.upper(), empty_bars())
        if self._current_ts is not None and not df.empty:
            df = df[df.index <= pd.Timestamp(self._current_ts)]
        if timeframe != "1Min":
            df = resample_bars(df, timeframe)
        return df.iloc[-limit:] if limit else df

    def last_price(self, symbol: str) -> float | None:
        df = self.frame(symbol, "1Min", limit=1)
        return None if df.empty else float(df["close"].iloc[-1])

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {s.upper(): p for s in symbols if (p := self.last_price(s)) is not None}

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s.upper(): Quote(symbol=s.upper(), price=round(p, 2)) for s in symbols if (p := self.last_price(s)) is not None}

    async def history(self, symbol: str, timeframe: str, start: datetime, end: datetime, **_: Any) -> pd.DataFrame:
        df = self.frame(symbol, "1Min")
        if df.empty:
            return df
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        if start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize("UTC")
        if end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize("UTC")
        end_ts = min(end_ts, pd.Timestamp(self._current_ts)) if self._current_ts is not None else end_ts
        sliced = df[(df.index >= start_ts) & (df.index <= end_ts)]
        return resample_bars(sliced, timeframe) if timeframe != "1Min" else sliced

    async def close(self) -> None:
        return None
