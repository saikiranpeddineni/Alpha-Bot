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
from app.data.base import empty_bars, normalize_bars, resample_bars


def _bar_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build the same canonical OHLCV shape production DataManager exposes."""
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
    if not records:
        return empty_bars()
    return normalize_bars(pd.DataFrame(records, index=pd.DatetimeIndex(indexes, name="timestamp")))


class ReplayMarketFeed:
    def __init__(self, data_path: Path | None = None) -> None:
        # Only the implicit/default replay source may use the legacy July-24 fallback.
        # An explicitly supplied path is an isolation contract: if it is absent, replay
        # must fail closed instead of silently substituting some other historical file.
        self._explicit_data_path = data_path is not None
        self.data_path = data_path or (DATA_DIR / "replay_bars.jsonl")
        self.symbols: set[str] = set()

    def load_bars(self, start_date: str, end_date: str, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.data_path.exists() and not self._explicit_data_path:
            alt = Path(__file__).resolve().parents[3] / "scratch" / "july24_replay_stream.jsonl"
            if alt.exists():
                self.data_path = alt
        if not self.data_path.exists():
            raise FileNotFoundError(f"Replay market data not found: {self.data_path}")

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
                if not symbol:
                    continue
                raw_ts = row.get("ts") or row.get("t")
                if not raw_ts:
                    raise ValueError(f"Replay bar missing timestamp at line {line_no}")
                dt = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else raw_ts
                if not isinstance(dt, datetime):
                    raise ValueError(f"Unsupported timestamp at line {line_no}: {raw_ts!r}")
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.strftime("%Y-%m-%d") > end_date:
                    continue
                normalized = dict(row, ts_dt=dt, symbol=symbol)
                normalized["in_replay_window"] = start_date <= dt.strftime("%Y-%m-%d") <= end_date
                rows.append(normalized)
                self.symbols.add(symbol)

        deduped: dict[tuple[str, datetime], dict[str, Any]] = {}
        for row in rows:
            deduped[(row["symbol"], row["ts_dt"])] = row
        rows = sorted(deduped.values(), key=lambda r: (r["ts_dt"], r["symbol"]))
        if not any(r["in_replay_window"] for r in rows):
            raise ValueError(f"No replay bars inside {start_date}..{end_date} symbols={symbols or 'ALL'}")

        present = {r["symbol"] for r in rows}
        missing = [s for s in ("SPY", "QQQ") if s not in present]
        if "VIX" not in present and "^VIX" not in present:
            missing.append("VIX/^VIX")
        if missing:
            raise ValueError(
                "Replay production-fidelity input incomplete: missing historical "
                f"market-risk series {', '.join(missing)}. Refusing conservative/UNKNOWN substitution."
            )
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
        for bar in bars: grouped.setdefault(bar["symbol"], []).append(bar)
        self._bars = {symbol: sorted(items, key=lambda x: x["ts_dt"]) for symbol, items in grouped.items()}
        self._now = min(bar["ts_dt"] for bar in bars)

    def set_time(self, ts: datetime) -> None:
        self._now = ts

    def _symbol(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol == "VIX" and "VIX" not in self._bars and "^VIX" in self._bars:
            return "^VIX"
        return symbol

    def _visible(self, symbol: str) -> list[dict[str, Any]]:
        return [row for row in self._bars.get(self._symbol(symbol), []) if row["ts_dt"] <= self._now]

    def frame(self, symbol: str, timeframe: str = "1Min", limit: int | None = None) -> pd.DataFrame:
        """Production DataManager.frame parity over the strict as-of replay view.

        Only bars at or before the replay clock are exposed, so callers such as scanner,
        market-risk and quote payload code can use their normal production data path
        without introducing lookahead.
        """
        df = _bar_frame(self._visible(symbol))
        if timeframe != "1Min":
            df = resample_bars(df, timeframe)
        if limit:
            df = df.iloc[-limit:]
        return df

    def last_price(self, symbol: str) -> float | None:
        df = self.frame(symbol, "1Min", limit=1)
        if df.empty:
            return None
        return float(df["close"].iloc[-1])

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for symbol in symbols:
            price = self.last_price(symbol)
            if price is not None:
                out[symbol.upper()] = price
        return out

    def latest_quote(self, symbol: str) -> Quote | None:
        price = self.last_price(symbol)
        if price is None:
            return None
        return Quote(symbol=symbol, bid=price, ask=price, last=price, timestamp=self._now)

    async def history(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        rows = [row for row in self._visible(symbol) if start <= row["ts_dt"] <= min(end, self._now)]
        frame = _bar_frame(rows)
        if timeframe.lower() not in {"1min", "1m", "1minute"}:
            frame = resample_bars(frame, timeframe)
        return frame.tail(self.max_rows)

    def fidelity_diagnostics(self) -> dict[str, Any]:
        present = set(self._bars)
        return {"symbols": sorted(present), "has_spy": "SPY" in present, "has_qqq": "QQQ" in present,
                "has_vix": "VIX" in present or "^VIX" in present, "lookahead_policy": "strict_as_of"}
