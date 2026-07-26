"""Deterministic historical market-data adapter for production replay."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from app.config import DATA_DIR
from app.core.domain import Quote
from app.data.base import empty_bars, resample_bars


def _bar_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return empty_bars()
    records, indexes = [], []
    for row in rows:
        close = row.get("close", row.get("c"))
        if close is None:
            continue
        records.append({
            "open": float(row.get("open", row.get("o", close))),
            "high": float(row.get("high", row.get("h", close))),
            "low": float(row.get("low", row.get("l", close))),
            "close": float(close), "volume": float(row.get("volume", row.get("v", 0.0))),
        })
        indexes.append(pd.Timestamp(row["ts_dt"]))
    return pd.DataFrame(records, index=pd.DatetimeIndex(indexes, name="timestamp")).sort_index()


class ReplayMarketFeed:
    def __init__(self, data_path: Path | None = None) -> None:
        self.data_path = data_path or (DATA_DIR / "replay_bars.jsonl")
        self.symbols: set[str] = set()

    def load_bars(self, start_date: str, end_date: str, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.data_path.exists():
            alt = Path(__file__).resolve().parents[3] / "scratch" / "july24_replay_stream.jsonl"
            if alt.exists():
                self.data_path = alt
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
                    raise ValueError(f"Invalid replay JSONL line {line_no}: {exc}") from exc
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
                    raise ValueError(f"Unsupported timestamp at line {line_no}: {raw_ts!r}")
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                # Keep all historical rows present in the immutable artifact up
                # to end_date. ReplayRuntime selects which timestamps are ticks;
                # earlier rows are warmup/scanner history only.
                if dt.strftime("%Y-%m-%d") > end_date:
                    continue
                normalized = dict(row, ts_dt=dt, symbol=symbol)
                normalized["in_replay_window"] = start_date <= dt.strftime("%Y-%m-%d") <= end_date
                rows.append(normalized)
                self.symbols.add(symbol)
        rows.sort(key=lambda r: (r["ts_dt"], r["symbol"]))
        if not any(r["in_replay_window"] for r in rows):
            raise ValueError(f"No replay bars inside {start_date}..{end_date} symbols={symbols or 'ALL'}")
        return rows


class ReplayDataManager:
    """DataManager-compatible, immutable, no-lookahead historical view."""
    def __init__(self, bars: list[dict[str, Any]], max_rows: int = 3000) -> None:
        if not bars:
            raise ValueError("ReplayDataManager requires historical bars")
        self.provider = SimpleNamespace(name="replay", supports_asset_discovery=False)
        self.max_rows = max_rows
        self.cache_policy = "immutable_replay"
        self.provenance: dict[str, dict[str, Any]] = {}
        self._pending_by_ts: dict[datetime, dict[str, list[dict[str, Any]]]] = {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for bar in bars:
            symbol, ts = bar["symbol"].upper(), bar["ts_dt"]
            grouped.setdefault(symbol, []).append(bar)
            if bar.get("in_replay_window", True):
                self._pending_by_ts.setdefault(ts, {}).setdefault(symbol, []).append(bar)
        self._all_frames = {symbol: _bar_frame(rows) for symbol, rows in grouped.items()}
        self._current_ts: datetime | None = None
        self._delivered_ts: set[datetime] = set()

    @property
    def is_synthetic(self) -> bool:
        return False

    async def warmup(self, symbols: list[str], lookback_bars: int = 800) -> None:
        return None

    def set_time(self, ts: datetime) -> None:
        self._current_ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    def _asof(self, symbol: str) -> pd.DataFrame:
        df = self._all_frames.get(symbol.upper(), empty_bars())
        if self._current_ts is None or df.empty:
            return df.iloc[0:0] if self._current_ts is None else df
        return df[df.index <= pd.Timestamp(self._current_ts)]

    async def poll(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        if self._current_ts is None or self._current_ts in self._delivered_ts:
            return {}
        self._delivered_ts.add(self._current_ts)
        requested = {s.upper() for s in symbols} | {"VIX", "^VIX"}
        batch = self._pending_by_ts.get(self._current_ts, {})
        return {symbol: _bar_frame(rows) for symbol, rows in sorted(batch.items()) if symbol in requested}

    def frame(self, symbol: str, timeframe: str = "1Min", limit: int | None = None) -> pd.DataFrame:
        df = self._asof(symbol)
        if timeframe != "1Min":
            df = resample_bars(df, timeframe)
        return df.iloc[-limit:] if limit else df

    def last_price(self, symbol: str) -> float | None:
        df = self.frame(symbol, "1Min", 1)
        return None if df.empty else float(df["close"].iloc[-1])

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {s.upper(): p for s in symbols if (p := self.last_price(s)) is not None}

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s.upper(): Quote(symbol=s.upper(), price=round(p, 2), ts=self._current_ts) for s in symbols if (p := self.last_price(s)) is not None}

    async def history(self, symbol: str, timeframe: str, start: datetime, end: datetime, **_: Any) -> pd.DataFrame:
        df = self._asof(symbol)
        if df.empty:
            return df
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        if start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize("UTC")
        if end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize("UTC")
        if self._current_ts is not None:
            end_ts = min(end_ts, pd.Timestamp(self._current_ts))
        df = df[(df.index >= start_ts) & (df.index <= end_ts)]
        return resample_bars(df, timeframe) if timeframe != "1Min" else df

    async def close(self) -> None:
        return None
