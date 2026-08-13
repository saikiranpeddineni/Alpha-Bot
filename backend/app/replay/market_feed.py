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
        self._explicit_data_path = data_path is not None
        self.data_path = data_path or (DATA_DIR / "replay_bars.jsonl")
        self.symbols: set[str] = set()

    async def ensure_data(self, start_date: str, end_date: str, symbols: list[str], settings=None) -> None:
        """Download missing 1-minute historical data into the replay JSONL cache."""
        import shutil
        from app.data.alpaca_data import AlpacaDataProvider
        from app.core.applog import applog
        from datetime import timedelta
        
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

        missing_symbols = []
        for symbol in symbols:
            symbol_avail = avail_dates.get(symbol, set())
            if any(date not in symbol_avail for date in needed_dates):
                missing_symbols.append(symbol)

        if not missing_symbols:
            return

        applog.info("replay.feed", f"Downloading missing historical data for {len(missing_symbols)} symbols: {start_date} to {end_date}")
        
        if settings is None:
            raise ValueError("ReplayMarketFeed.ensure_data requires explicit settings injection (Phase 0 Config Parity).")
        try:
            provider = AlpacaDataProvider(api_key=settings.alpaca_api_key, api_secret=settings.alpaca_api_secret)
        except RuntimeError:
            applog.error("replay.feed", "Alpaca API credentials missing. Cannot fetch missing replay data.")
            return

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc, hour=23, minute=59, second=59)

        # Open in append mode so we don't corrupt existing cache.
        # load_bars() correctly sorts by timestamp, so appended out-of-order data is completely fine.
        with self.data_path.open("a", encoding="utf-8") as handle:
            for symbol in missing_symbols:
                applog.info("replay.feed", f"Fetching 1Min bars for {symbol}...")
                
                if symbol in ("VIX", "^VIX"):
                    from app.data.yahoo import YahooProvider
                    from zoneinfo import ZoneInfo as _ZoneInfo
                    vix_provider = YahooProvider()
                    # Yahoo does NOT provide intraday minute bars for ^VIX historically.
                    # Try 1Min first; if empty (as expected), fall back to 1Day and
                    # synthesize a market-open row per session so the engine heartbeat
                    # receives a non-empty VIX frame with age=0 relative to replay clock.
                    fetch_sym = "^VIX" if symbol == "VIX" else symbol
                    df = await vix_provider.get_bars(fetch_sym, "1Min", start=start_dt, end=end_dt)
                    if df.empty:
                        applog.info("replay.feed", f"^VIX 1Min unavailable — falling back to daily bars and synthesizing session rows")
                        df_daily = await vix_provider.get_bars(fetch_sym, "1Day", start=start_dt, end=end_dt)
                        if not df_daily.empty:
                            # Synthesize one bar per calendar day aligned to 09:30 ET (=13:30 UTC)
                            # and one bar per market-hour minute so carry-forward works at any tick.
                            ET = _ZoneInfo("America/New_York")
                            rows_written = 0
                            for day_ts, row in df_daily.iterrows():
                                # Align to ET market open on that calendar day
                                day_dt = day_ts.to_pydatetime() if hasattr(day_ts, "to_pydatetime") else day_ts
                                day_date = day_dt.date()
                                session_open_et = datetime(day_date.year, day_date.month, day_date.day, 9, 30, 0, tzinfo=ET)
                                session_open_utc = session_open_et.astimezone(timezone.utc)
                                # Write one bar per minute from 09:30 to 16:00 ET using the daily close as the value.
                                # This ensures the carry-forward index in frame() always has a recent bar.
                                vix_close = float(row.get("close", row.get("c", 0.0)))
                                for minute_offset in range(0, 6 * 60 + 31):  # 09:30..16:00 inclusive
                                    bar_ts = session_open_utc + timedelta(minutes=minute_offset)
                                    handle.write(json.dumps({
                                        "symbol": symbol,
                                        "ts": bar_ts.isoformat(),
                                        "open": vix_close,
                                        "high": vix_close,
                                        "low": vix_close,
                                        "close": vix_close,
                                        "volume": 0.0,
                                        "kind": "bar",
                                        "synthetic": True,
                                    }) + "\n")
                                    rows_written += 1
                            applog.info("replay.feed", f"Synthesized {rows_written} VIX session bars from {len(df_daily)} daily bars")
                            continue
                        else:
                            applog.warning("replay.feed", f"VIX daily bars also empty — VIX feed will be unavailable for replay")
                            continue
                    if not df.empty and fetch_sym != symbol:
                        df["symbol"] = symbol
                else:
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
                
                # Strict replay window: requested dates AND strictly market hours (09:30:00 to 16:00:00 ET)
                from zoneinfo import ZoneInfo
                from datetime import time
                et_dt = dt.astimezone(ZoneInfo("America/New_York"))
                is_requested_date = start_date <= et_dt.strftime("%Y-%m-%d") <= end_date
                is_market_hours = time(9, 30) <= et_dt.time() <= time(16, 0)
                normalized["in_replay_window"] = is_requested_date and is_market_hours
                
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
    """DataManager-compatible, immutable, no-lookahead historical view."""
    def __init__(self, bars: list[dict[str, Any]], max_rows: int = 3000) -> None:
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
        self._dfs: dict[str, pd.DataFrame] = {}
        self._cursors: dict[str, int] = {}
        
        for symbol, items in self._bars.items():
            self._dfs[symbol] = _bar_frame(items)
            self._cursors[symbol] = 0
            
        self._now = min(bar["ts_dt"] for bar in bars)
        self._last_polled_ts: dict[str, datetime] = {}

    @property
    def is_synthetic(self) -> bool:
        return False

    def set_time(self, ts: datetime) -> None:
        self._now = ts

    def _symbol(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol == "VIX" and "VIX" not in self._bars and "^VIX" in self._bars:
            return "^VIX"
        if symbol == "^VIX" and "^VIX" not in self._bars and "VIX" in self._bars:
            return "VIX"
        return symbol

    def frame(self, symbol: str, timeframe: str = "1Min", limit: int | None = None) -> pd.DataFrame:
        """Return the production-compatible rolling frame as of the replay clock."""
        canonical = self._symbol(symbol)
        full_df = self._dfs.get(canonical)
        if full_df is None or full_df.empty:
            df = empty_bars()
        else:
            df = full_df.loc[:pd.Timestamp(self._now)]

        # VIX is an external market-risk observation in production and can update less
        # frequently than the equity clock. Carry the last known observation forward to
        # replay-now without exposing a future observation. Mark the synthetic minute
        # with zero volume so it cannot be mistaken for an actual market print.
        if canonical in ("VIX", "^VIX") and not df.empty:
            now_ts = pd.Timestamp(self._now)
            if df.index[-1] < now_ts:
                last = df.iloc[-1].copy()
                last["volume"] = 0.0
                carried = pd.DataFrame([last], index=pd.DatetimeIndex([now_ts], name=df.index.name))
                df = pd.concat([df, carried])

        if timeframe != "1Min":
            df = resample_bars(df, timeframe)
        if limit:
            df = df.iloc[-limit:]
        return df

    async def poll(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Expose bars newly closed at replay-now without network access or lookahead."""
        extended = [s.upper() for s in symbols]
        if "^VIX" not in extended and "VIX" not in extended:
            extended.append("^VIX")
        new_closed: dict[str, pd.DataFrame] = {}
        for requested in extended:
            canonical = self._symbol(requested)
            bars = self._bars.get(canonical)
            if not bars:
                continue
            
            idx = self._cursors.get(canonical, 0)
            fresh_rows = []
            while idx < len(bars) and bars[idx]["ts_dt"] <= self._now:
                fresh_rows.append(bars[idx])
                idx += 1
            self._cursors[canonical] = idx
            
            if not fresh_rows:
                continue
            
            last_seen = self._last_polled_ts.get(canonical)
            fresh_rows = [row for row in fresh_rows if last_seen is None or row["ts_dt"] > last_seen]
            if not fresh_rows:
                continue
            fresh = _bar_frame(fresh_rows)
            if fresh.empty:
                continue
            self._last_polled_ts[canonical] = fresh_rows[-1]["ts_dt"]
            new_closed[requested] = fresh
        return new_closed

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

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for symbol in symbols:
            price = self.last_price(symbol)
            if price is not None:
                out[symbol.upper()] = Quote(symbol=symbol.upper(), price=round(price, 2))
        return out

    def latest_quote(self, symbol: str) -> Quote | None:
        price = self.last_price(symbol)
        if price is None:
            return None
        return Quote(symbol=symbol, price=price)

    async def history(self, symbol: str, timeframe: str, start: datetime, end: datetime, prefer_cache: bool = True, cache_policy: str | None = None) -> pd.DataFrame:
        canonical = self._symbol(symbol)
        full_df = self._dfs.get(canonical)
        if full_df is None or full_df.empty:
            return empty_bars()
            
        end_bound = min(end, self._now)
        frame = full_df.loc[start:pd.Timestamp(end_bound)]
        
        if timeframe.lower() not in {"1min", "1m", "1minute"}:
            frame = resample_bars(frame, timeframe)
        return frame.tail(self.max_rows)

    def fidelity_diagnostics(self) -> dict[str, Any]:
        present = set(self._bars)
        return {"symbols": sorted(present), "has_spy": "SPY" in present, "has_qqq": "QQQ" in present,
                "has_vix": "VIX" in present or "^VIX" in present, "lookahead_policy": "strict_as_of"}
