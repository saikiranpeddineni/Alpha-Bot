"""Deterministic historical market-data adapter for production replay."""
from __future__ import annotations

import json
from datetime import UTC, datetime
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
        self._explicit_data_path = data_path is not None
        self.data_path = data_path or (DATA_DIR / "replay_bars.jsonl")
        self.symbols: set[str] = set()

    async def ensure_data(self, start_date: str, end_date: str, symbols: list[str]) -> None:
        """Download missing 1-minute historical data into the replay JSONL cache."""
        import shutil
        from datetime import timedelta

        from app.config import get_settings
        from app.core.applog import applog

        # Check if fallback scratch file should be copied to establish initial cache
        if not self.data_path.exists() and not self._explicit_data_path:
            alt = Path(__file__).resolve().parents[3] / "scratch" / "july24_replay_stream.jsonl"
            if alt.exists():
                shutil.copy(alt, self.data_path)
                applog.info("replay.feed", f"Copied base replay snapshot to {self.data_path.name}")
            else:
                self.data_path.parent.mkdir(parents=True, exist_ok=True)
                self.data_path.touch()

        # Parse existing JSONL cache to determine missing symbols/dates
        avail_dates: dict[str, set[str]] = {}
        with self.data_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip(): continue
                try:
                    row = json.loads(line)
                    sym = str(row.get("symbol", "")).upper()
                    ts = row.get("ts") or row.get("t")
                    if sym and ts:
                        avail_dates.setdefault(sym, set()).add(ts[:10])
                except Exception:
                    continue

        # Calculate needed dates (excluding weekends)
        needed_dates = []
        d = datetime.strptime(start_date, "%Y-%m-%d")
        end_d = datetime.strptime(end_date, "%Y-%m-%d")
        while d <= end_d:
            if d.weekday() < 5:
                needed_dates.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
        if not needed_dates:
            return

        requested_symbols = []
        for symbol in symbols:
            normalized = str(symbol).upper()
            requested_symbols.append("^VIX" if normalized == "VIX" else normalized)
        required_symbols = list(dict.fromkeys([
            *requested_symbols,
            "SPY",
            "QQQ",
            "^VIX",
        ]))
        missing_symbols = []
        for symbol in required_symbols:
            # ReplayDataManager treats VIX and ^VIX as aliases. Count either
            # spelling as coverage so a cache does not download a duplicate
            # risk series merely because its source used another spelling.
            if symbol in {"VIX", "^VIX"}:
                symbol_avail = avail_dates.get("VIX", set()) | avail_dates.get("^VIX", set())
                # Yahoo only exposes a short 1-minute history. A risk series
                # present somewhere in the requested range is therefore the
                # useful repair criterion; requiring every warmup date would
                # redownload the same bounded Yahoo window on every replay.
                has_coverage = any(start_date <= date <= end_date for date in symbol_avail)
            else:
                symbol_avail = avail_dates.get(symbol, set())
                has_coverage = all(date in symbol_avail for date in needed_dates)
            if not has_coverage:
                missing_symbols.append(symbol)

        if not missing_symbols:
            return

        applog.info("replay.feed", f"Downloading missing historical data for {len(missing_symbols)} symbols: {start_date} to {end_date}")

        settings = get_settings()

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=UTC, hour=23, minute=59, second=59)

        # Production uses Yahoo for VIX while equity bars come from Alpaca.
        # Replay must build the same market-risk context instead of attempting
        # to fetch ^VIX through the stock-bars client (which returns no data).
        alpaca_provider = None
        yahoo_provider = None
        if any(symbol not in {"VIX", "^VIX"} for symbol in missing_symbols):
            try:
                from app.data.alpaca_data import AlpacaDataProvider

                alpaca_provider = AlpacaDataProvider(
                    api_key=settings.alpaca_api_key,
                    api_secret=settings.alpaca_api_secret,
                )
            except RuntimeError as exc:
                applog.error("replay.feed", f"Alpaca replay repair unavailable: {exc}")

        if any(symbol in {"VIX", "^VIX"} for symbol in missing_symbols):
            try:
                from app.data.yahoo import YahooProvider

                yahoo_provider = YahooProvider()
            except RuntimeError as exc:
                applog.error("replay.feed", f"Yahoo VIX replay repair unavailable: {exc}")

        # Open in append mode so we don't corrupt existing cache.
        # load_bars() correctly sorts by timestamp, so appended out-of-order data is completely fine.
        with self.data_path.open("a", encoding="utf-8") as handle:
            for symbol in missing_symbols:
                applog.info("replay.feed", f"Fetching 1Min bars for {symbol}...")
                provider = yahoo_provider if symbol in {"VIX", "^VIX"} else alpaca_provider
                if provider is None:
                    applog.warning("replay.feed", f"No provider available for {symbol}; leaving replay input incomplete")
                    continue
                df = await provider.get_bars(symbol, "1Min", start=start_dt, end=end_dt)
                if df.empty:
                    applog.warning("replay.feed", f"No data returned for {symbol}")
                    continue

                rows_written = 0
                for ts, row in df.iterrows():
                    handle.write(json.dumps({
                        "symbol": symbol,
                        "ts": ts.isoformat(),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                        "kind": "bar"
                    }) + "\n")
                    rows_written += 1
                applog.info("replay.feed", f"Appended {rows_written} bars for {symbol}")

    def load_bars(self, start_date: str, end_date: str, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.data_path.exists() and not self._explicit_data_path:
            alt = Path(__file__).resolve().parents[3] / "scratch" / "july24_replay_stream.jsonl"
            if alt.exists():
                self.data_path = alt
        if not self.data_path.exists():
            raise FileNotFoundError(f"Replay market data not found: {self.data_path}")

        # Honor the requested universe. Market-risk series must survive any
        # filter or the production-fidelity input guard below would trip.
        symbol_filter = {s.upper() for s in symbols} if symbols else None
        if symbol_filter is not None:
            symbol_filter |= {"SPY", "QQQ", "VIX", "^VIX"}

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
                if symbol_filter is not None and symbol not in symbol_filter:
                    continue
                raw_ts = row.get("ts") or row.get("t")
                if not raw_ts:
                    raise ValueError(f"Replay bar missing timestamp at line {line_no}")
                dt = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else raw_ts
                if not isinstance(dt, datetime):
                    raise ValueError(f"Unsupported timestamp at line {line_no}: {raw_ts!r}")
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
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
            avail_dates = sorted({r["ts_dt"].strftime("%Y-%m-%d") for r in rows})
            msg = f"No replay bars inside {start_date}..{end_date} symbols={symbols or 'ALL'}"
            if avail_dates:
                msg += f". Available replay data date range: {avail_dates[0]}..{avail_dates[-1]}"
            raise ValueError(msg)

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
    """DataManager-compatible, immutable, no-lookahead historical view.

    PERF (Option A): each symbol's full history is converted to an enriched
    DataFrame ONCE at construction; every later ``frame()`` is a bisect slice
    returning a zero-copy view. The old implementation rebuilt DataFrames from
    raw dicts and linear-scanned all bars on EVERY call -- the dominant cost.
    """

    def __init__(self, bars: list[dict[str, Any]], max_rows: int = 3000, pre_enrich: bool = True) -> None:
        if not bars:
            raise ValueError("ReplayDataManager requires historical bars")
        self.provider = SimpleNamespace(name="replay", supports_asset_discovery=False)
        self.max_rows = max_rows
        self.cache_policy = "immutable_replay"
        self.provenance: dict[str, dict[str, Any]] = {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for bar in bars:
            grouped.setdefault(bar["symbol"].upper(), []).append(bar)
        self._bars = {symbol: sorted(items, key=lambda x: x["ts_dt"]) for symbol, items in grouped.items()}
        self._now = min(bar["ts_dt"] for bar in bars)
        self._last_polled_ts: dict[str, datetime] = {}

        # ---- Option A pre-computation ----
        from app.indicators.ta import enrich
        from app.ai.features import compute_features
        self._frames: dict[str, pd.DataFrame] = {}
        self._ts_index: dict[str, list[datetime]] = {}
        for symbol, items in self._bars.items():
            df = _bar_frame(items)
            if pre_enrich and not df.empty:
                try:
                    df = enrich(df)
                    df = compute_features(df)
                except Exception:
                    pass  # fall back to raw frame; enrich() inside prod path will handle it
            self._frames[symbol] = df
            self._ts_index[symbol] = [b["ts_dt"] for b in items]
        # alias map for VIX naming
        self._alias: dict[str, str] = {}
        for sym in self._frames:
            if sym == "^VIX" and "VIX" not in self._frames:
                self._alias["VIX"] = "^VIX"
            if sym == "VIX" and "^VIX" not in self._frames:
                self._alias["^VIX"] = "VIX"

    @property
    def available_symbols(self) -> frozenset[str]:
        """Symbols with historical bars in this immutable replay input."""
        return frozenset(self._bars)

    @property
    def is_synthetic(self) -> bool:
        return False

    async def warmup(self, symbols: list[str], lookback_bars: int = 800) -> None:
        """No-op: all bars are preloaded and enriched at construction."""
        return None

    # =============================================================================
    # [MODIFIED 2026-10-13]: Add backfill method to ReplayDataManager
    # ORIGINAL INTENT: ReplayDataManager historically did not define backfill();
    #                  only warmup() was stubbed as a no-op because bars are preloaded.
    # REASON FOR CHANGE: TradingEngineEvaluationMixin._evaluate_symbol schedules
    #                    await self.data.backfill(sym) when a thin frame is encountered.
    #                    Without backfill(), an unhandled AttributeError was raised.
    # SAFE ROLLBACK: ReplayDataManager remains immutable; backfill() returns False
    #                without performing any network I/O or modifying preloaded frames.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    async def backfill(self, symbol: str, bars: int | None = None) -> bool:
        """No-op in replay: all historical bars are preloaded and immutable."""
        return False
    # =============================================================================

    def set_time(self, ts: datetime) -> None:
        self._now = ts

    def _canonical(self, symbol: str) -> str:
        s = symbol.upper()
        return self._alias.get(s, s)

    def _cutoff_idx(self, symbol: str) -> int:
        """Last index with ts_dt <= now via bisect over the ts index."""
        import bisect
        idx = self._ts_index.get(symbol)
        if idx is None:
            return -1
        return bisect.bisect_right(idx, self._now) - 1

    def frame(self, symbol: str, timeframe: str = "1Min", limit: int | None = None) -> pd.DataFrame:
        canonical = self._canonical(symbol.upper())
        i = self._cutoff_idx(canonical)
        if i < 0:
            return empty_bars()
        lo = 0 if limit is None else max(0, i - int(limit) + 1)
        df = self._frames[canonical].iloc[lo : i + 1]  # zero-copy view

        if canonical in ("VIX", "^VIX") and len(df):
            now_ts = pd.Timestamp(self._now)
            if df.index[-1] < now_ts:
                last = df.iloc[-1].copy()
                last["volume"] = 0.0
                carried = pd.DataFrame([last], index=pd.DatetimeIndex([now_ts], name=df.index.name))
                df = pd.concat([df, carried])

        if timeframe != "1Min":
            df = resample_bars(df, timeframe)
        elif limit is not None:
            df = df.tail(int(limit))
        return df

    async def poll(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Bars newly closed at replay-now (bisect deltas, tiny frames).

        Slices the precomputed enriched frames instead of rebuilding raw
        frames per symbol -- _bar_frame/normalize_bars showed up as a top
        profile cost when called once per symbol per tick.
        """
        extended = [s.upper() for s in symbols]
        if "^VIX" not in extended and "VIX" not in extended:
            extended.append("^VIX")
        new_closed: dict[str, pd.DataFrame] = {}
        import bisect
        for requested in extended:
            canonical = self._canonical(requested)
            idx_all = self._ts_index.get(canonical)
            if not idx_all:
                continue
            last_seen = self._last_polled_ts.get(canonical)
            lo = 0 if last_seen is None else bisect.bisect_right(idx_all, last_seen)
            hi = self._cutoff_idx(canonical)
            if lo > hi:
                continue
            fresh_rows = self._bars[canonical][lo : hi + 1]
            if not fresh_rows:
                continue
            frame_all = self._frames.get(canonical)
            if frame_all is not None and len(frame_all) == len(idx_all):
                fresh = frame_all.iloc[lo : hi + 1]
            else:  # pragma: no cover - fallback for exotic frames
                fresh = _bar_frame(fresh_rows)
            if fresh.empty:
                continue
            self._last_polled_ts[canonical] = fresh_rows[-1]["ts_dt"]
            new_closed[requested] = fresh
        return new_closed

    def last_price(self, symbol: str) -> float | None:
        canonical = self._canonical(symbol.upper())
        i = self._cutoff_idx(canonical)
        if i < 0:
            return None
        return float(self._frames[canonical]["close"].iloc[i])

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for symbol in symbols:
            price = self.last_price(symbol)
            if price is not None:
                out[symbol.upper()] = price
        return out

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for symbol in symbols:
            q = self.latest_quote(symbol)
            if q is not None:
                out[symbol.upper()] = q
        return out

    def latest_quote(self, symbol: str) -> Quote | None:
        price = self.last_price(symbol)
        if price is None:
            return None
        return Quote(symbol=symbol.upper(), price=price)

    async def history(self, symbol: str, timeframe: str, start: datetime, end: datetime, prefer_cache: bool = True, cache_policy: str | None = None) -> pd.DataFrame:
        canonical = self._canonical(symbol.upper())
        import bisect
        idx_all = self._ts_index.get(canonical)
        if not idx_all:
            return empty_bars()
        hi_ts = min(end, self._now)
        lo = bisect.bisect_left(idx_all, start)
        hi = bisect.bisect_right(idx_all, hi_ts)
        df = self._frames[canonical].iloc[lo:hi]
        if timeframe.lower() not in {"1min", "1m", "1minute"}:
            df = resample_bars(df, timeframe)
        return df.tail(self.max_rows)

    def fidelity_diagnostics(self) -> dict[str, Any]:
        present = set(self._bars)
        return {"symbols": sorted(present), "has_spy": "SPY" in present, "has_qqq": "QQQ" in present,
                "has_vix": "VIX" in present or "^VIX" in present, "lookahead_policy": "strict_as_of"}
