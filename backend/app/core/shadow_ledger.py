"""Shadow ledger: records every REJECTED entry signal and resolves what
price would have done afterwards.

Purpose: quantify missed winners / avoided losers per gate (pipeline vetoes,
ML/meta gates, Trade Score, portfolio gate, behavioral detector) without
touching any trading decision. Purely additive:

- record() is O(1): builds a row and hands it to a background writer thread.
  It never raises and never blocks on the event loop.
- Resolution runs in a periodic asyncio task that reads bars off the existing
  data manager (read-only) and updates pending rows.

Nothing in this module feeds back into sizing, gating, or execution.
"""
from __future__ import annotations

import asyncio
import os
import queue
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.applog import applog

_DEBUG_FILE = Path(os.environ.get("SHADOW_TRACE", "")) if os.environ.get("SHADOW_TRACE") else None


def _dbg(msg: str) -> None:
    if _DEBUG_FILE:
        try:
            with open(_DEBUG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now(UTC).strftime('%H:%M:%S.%f')[:-3]} [{threading.current_thread().name}] {msg}\n")
        except Exception:
            pass

# Default location lives beside the trading DB. Override with
# ALPHADESK_SHADOW_LEDGER_DB (tests MUST redirect this: engine-level tests
# otherwise wrote rejection forensics into the production ledger -- observed
# 2026-08-24 as a 'test_strat' AAPL row inside prod data).
_DB_PATH = Path(
    os.environ.get("ALPHADESK_SHADOW_LEDGER_DB")
    or (Path(__file__).resolve().parents[2] / "data" / "shadow_ledger.db")
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT,
    action TEXT,
    signal_price REAL,
    stop_loss REAL,
    take_profit REAL,
    atr14 REAL,
    confidence REAL,
    ml_probability REAL,
    meta_prob REAL,
    trade_score REAL,
    candidate_id TEXT,
    gate TEXT NOT NULL,
    reason TEXT,
    -- resolution
    resolved INTEGER NOT NULL DEFAULT 0,
    px_5m REAL, px_15m REAL, px_30m REAL, px_60m REAL,
    fav_pct_60m REAL,
    mfe_pct_60m REAL, mae_pct_60m REAL,
    r_fav_60m REAL,
    verdict TEXT
);
CREATE INDEX IF NOT EXISTS idx_shadow_created ON shadow_entries(created_at);
CREATE INDEX IF NOT EXISTS idx_shadow_resolved ON shadow_entries(resolved);
"""

_META_PROB_RE = re.compile(r"prob[_ ]([01]\.\d+)")
_TRADE_SCORE_RE = re.compile(r"Trade Score (\d+(?:\.\d+)?)")

# verdict thresholds (favorable move over next 60 minutes)
WINNER_PCT = 0.5
WINNER_R = 0.75


def _parse_meta_prob(reason: str) -> float | None:
    m = _META_PROB_RE.search(reason or "")
    return float(m.group(1)) if m else None


def _parse_trade_score(reason: str) -> float | None:
    m = _TRADE_SCORE_RE.search(reason or "")
    return float(m.group(1)) if m else None


class ShadowLedger:
    """Fire-and-forget recorder + background resolver."""

    _initialized_paths: set[Path] = set()
    _init_lock = threading.Lock()

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._queue: queue.Queue[dict] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Replay isolation: replays must not write rejection forensics into
        # the production ledger (2026-08-26: one replay deposited ~300 rows
        # that then stalled the resolver waiting on forward bars the cache
        # will never have). Runtime flips this off for the duration of a pass.
        self.enabled = True
        # Schema/WAL are applied exactly once per file, before any concurrent
        # connection exists. Running DDL on every connect caused persistent
        # 'database is locked' collisions between writer and readers.
        self._init_db(self._db_path)
        self._start_writer()

    @staticmethod
    def _plain_connect(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(path, timeout=10, check_same_thread=False)
        con.execute("PRAGMA busy_timeout=5000")
        return con

    @classmethod
    def _init_db(cls, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with cls._init_lock:
            if path in cls._initialized_paths:
                return
            con = cls._plain_connect(path)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
                con.commit()
            finally:
                con.close()
            cls._initialized_paths.add(path)

    # ------------------------------------------------------------------
    def record(
        self,
        *,
        symbol: str,
        action: str,
        strategy: str,
        gate: str,
        reason: str,
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        atr14: float | None = None,
        confidence: float | None = None,
        sig=None,
        extra_reason: str = "",
    ) -> None:
        """Capture one rejected entry signal. Never raises."""
        if not self.enabled:
            return
        try:
            now = datetime.now(UTC)
            reason_full = f"{reason}{(' | ' + extra_reason) if extra_reason else ''}"
            row = {
                "created_at": now.isoformat(),
                "session_date": now.date().isoformat(),
                "symbol": symbol.upper(),
                "strategy": strategy,
                "action": action,
                "signal_price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "atr14": atr14,
                "confidence": confidence,
                "ml_probability": getattr(sig, "ml_probability", None),
                "meta_prob": _parse_meta_prob(reason_full),
                "trade_score": _parse_trade_score(reason_full),
                "candidate_id": getattr(sig, "candidate_id", None),
                "gate": gate,
                "reason": reason_full[:500],
            }
            self._queue.put_nowait(row)
            _dbg(f"recorded {row['symbol']} {row['action']} gate={gate}")
        except Exception as ex:  # noqa: BLE001 - must never affect prod path
            _dbg(f"record EXC {ex!r}")

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        return self._plain_connect(self._db_path)

    def _start_writer(self) -> None:
        def _writer_loop() -> None:
            _dbg("writer thread started")
            try:
                con = self._connect()
            except Exception as ex:
                _dbg(f"writer connect FAILED: {ex!r}")
                return
            cols = ["created_at", "session_date", "symbol", "strategy", "action", "signal_price", "stop_loss", "take_profit", "atr14", "confidence", "ml_probability", "meta_prob", "trade_score", "candidate_id", "gate", "reason"]
            sql = (
                f"INSERT INTO shadow_entries ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)})"
            )
            while not self._stop.is_set():
                batch: list[dict] = []
                try:
                    batch.append(self._queue.get(timeout=2.0))
                    while len(batch) < 200:
                        batch.append(self._queue.get_nowait())
                except queue.Empty:
                    pass
                except Exception:
                    continue
                if not batch:
                    continue
                # Retry: dropping a batch on a transient lock would silently
                # lose rejection forensics forever.
                for attempt in range(5):
                    try:
                        con.executemany(sql, batch)
                        con.commit()
                        _dbg(f"committed {len(batch)} rows (attempt {attempt})")
                        break
                    except sqlite3.OperationalError as e:
                        if attempt == 4:
                            applog.warning("shadow", f"write batch dropped after retries: {e}")
                        else:
                            self._stop.wait(0.2 * (attempt + 1))
                    except Exception as e:  # noqa: BLE001
                        applog.warning("shadow", f"writer flush failed: {e}")
                        break

        self._thread = threading.Thread(target=_writer_loop, name="shadow-ledger", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    async def resolve_pending(self, data_manager, max_rows: int = 300) -> int:
        """Update forward-return fields for unresolved entries. Read-only w.r.t. prod."""
        if not self.enabled:
            return 0

        updated = 0

        def _fetch(symbol: str, target: datetime):
            try:
                df = data_manager.frame(symbol, "1Min", limit=600)
                if df is not None and not getattr(df, "empty", True):
                    idx = df.index
                    if getattr(idx, "tz", None) is None:
                        idx = idx.tz_localize(UTC)
                    if len(idx) and idx.min() <= target <= idx.max():
                        return df
                # Retro path: the rolling frame only holds the most recent
                # ~600 bars, so rows older than that (server restart, weekend)
                # could NEVER resolve and stuck unresolved forever. Pull the
                # decision window from cache-backed history instead.
                #
                # ASYNC BUG 2026-10-16: history() is a coroutine, but _fetch is
                # sync (runs via to_thread). Returning it un-awaited handed a
                # coroutine object back as "df", which always tested empty —
                # all 7,412 historical rows died as NO_BARS with zero real
                # verdicts. Drive it to completion here (worker thread has no
                # running loop), else fall to the raw cache read below.
                try:
                    import asyncio as _aio

                    _coro = data_manager.history(
                        symbol.upper(), "1Min",
                        target - timedelta(minutes=30), target + timedelta(minutes=90),
                    )
                    try:
                        _aio.get_running_loop()
                        _has_loop = True
                    except RuntimeError:
                        _has_loop = False
                    if _has_loop:
                        _coro.close()
                        raise RuntimeError("history needs a loop; using raw cache")
                    return _aio.run(_coro)
                except Exception:
                    # RESOLVER REPAIR 2026-10-16: history() enforces session
                    # coverage validation, which rejects partial/intraday
                    # sessions. Forensics is read-only: bypass the
                    # trading-path coverage gate and read raw cached bars
                    # directly. Partial windows still resolve (px_at clamps
                    # to the last available bar).
                    try:
                        cache = getattr(data_manager, "cache", None)
                        if cache is not None:
                            raw = cache.query(
                                symbol.upper(), "1Min",
                                target - timedelta(minutes=30),
                                target + timedelta(minutes=90),
                            )
                            if raw is not None and not getattr(raw, "empty", True):
                                return raw
                    except Exception:
                        pass
                    return df
            except Exception:
                return None

        con = await asyncio.to_thread(self._connect)
        errors = 0
        skipped = 0
        try:
            rows = await asyncio.to_thread(
                con.execute,
                "SELECT id, created_at, symbol, action, signal_price, stop_loss, atr14 "
                "FROM shadow_entries WHERE resolved = 0 ORDER BY id LIMIT ?",
                (max_rows,),
            )
            pending = rows.fetchall()
            for row_id, created_at, symbol, action, entry_px, stop_px, atr in pending:
                try:
                    dec_dt = datetime.fromisoformat(created_at)
                    age = datetime.now(UTC) - dec_dt
                    target = dec_dt.astimezone(UTC).replace(second=0, microsecond=0)
                    df = await asyncio.to_thread(_fetch, symbol, target)
                    if df is None or getattr(df, "empty", True):
                        if age > timedelta(hours=6):
                            await asyncio.to_thread(
                                con.execute,
                                "UPDATE shadow_entries SET resolved=1, verdict='NO_BARS' WHERE id=?",
                                (row_id,),
                            )
                            updated += 1
                        else:
                            skipped += 1
                        continue
                    idx = df.index
                    # Normalize the bar index to tz-aware UTC regardless of what
                    # the provider returns (naive UTC, aware, etc.). A mismatch
                    # here silently skipped every row (2026-08-24 finding).
                    if getattr(idx, "tz", None) is None:
                        idx = idx.tz_localize(UTC)
                    idx = idx.tz_convert(UTC)
                    # Positional lookup: get_loc breaks on duplicate timestamps.
                    start_loc = int(idx.searchsorted(target, side="left"))
                    if start_loc >= len(idx):
                        if age > timedelta(hours=6):
                            await asyncio.to_thread(
                                con.execute,
                                "UPDATE shadow_entries SET resolved=1, verdict='NO_BARS' WHERE id=?",
                                (row_id,),
                            )
                            updated += 1
                        else:
                            skipped += 1
                        continue
                    window = df.iloc[start_loc : start_loc + 61]
                    if len(window) < 2:
                        # Sparse forward bars (cache gaps on thin names): without
                        # this age-out the row waited forever and the cycle
                        # logged waiting=N resolved=0 every minute (2026-08-25).
                        if age > timedelta(hours=6):
                            await asyncio.to_thread(
                                con.execute,
                                "UPDATE shadow_entries SET resolved=1, verdict='NO_BARS' WHERE id=?",
                                (row_id,),
                            )
                            updated += 1
                        else:
                            skipped += 1
                        continue
                    closes = [float(x) for x in window["close"].iloc[:61]]
                    highs = [float(x) for x in window["high"].iloc[:61]]
                    lows = [float(x) for x in window["low"].iloc[:61]]
                    entry = entry_px if entry_px is not None else closes[0]

                    def px_at(k: int, _closes: list[float] = closes) -> float:
                        return _closes[k] if k < len(_closes) else _closes[-1]

                    p5, p15, p30, p60 = px_at(5), px_at(15), px_at(30), px_at(min(60, len(closes) - 1))
                    sign = 1.0 if str(action).upper().endswith("LONG") else -1.0
                    fav_pct = (p60 - entry) / entry * 100 * sign
                    if sign > 0:
                        mfe = (max(highs) - entry) / entry * 100
                        mae = (entry - min(lows)) / entry * 100
                    else:
                        mfe = (entry - min(lows)) / entry * 100
                        mae = (max(highs) - entry) / entry * 100
                    r_fav = None
                    if stop_px is not None and atr is not None:
                        risk = abs(entry - stop_px) or atr
                        r_fav = (p60 - entry) * sign / risk
                    elapsed_bars = len(window) - 1
                    fully_elapsed = elapsed_bars >= 60 or age > timedelta(hours=6)
                    if not fully_elapsed:
                        skipped += 1
                        continue
                    if fav_pct >= WINNER_PCT or (r_fav is not None and r_fav >= WINNER_R):
                        verdict = "MISSED_WINNER"
                    elif fav_pct <= -WINNER_PCT or (r_fav is not None and r_fav <= -WINNER_R):
                        verdict = "AVOIDED_LOSER"
                    else:
                        verdict = "NEUTRAL"
                    await asyncio.to_thread(
                        con.execute,
                        "UPDATE shadow_entries SET resolved=1, px_5m=?, px_15m=?, px_30m=?, "
                        "px_60m=?, fav_pct_60m=?, mfe_pct_60m=?, mae_pct_60m=?, r_fav_60m=?, "
                        "verdict=? WHERE id=?",
                        (
                            round(p5, 4), round(p15, 4), round(p30, 4), round(p60, 4),
                            round(fav_pct, 3), round(mfe, 3), round(mae, 3),
                            round(r_fav, 3) if r_fav is not None else None,
                            verdict, row_id,
                        ),
                    )
                    updated += 1
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    if errors <= 3:
                        applog.warning("shadow", f"resolve failed for row {row_id} ({symbol}): {e!r}")
            if updated or errors:
                await asyncio.to_thread(con.commit)
        finally:
            con.close()
        # Visibility: a fully silent cycle hid the tz bug for a full session.
        if pending and (updated or skipped or errors):
            applog.info(
                "shadow",
                f"cycle: pending={len(pending)} resolved={updated} waiting={skipped} errors={errors}",
            )
        return updated

    # ------------------------------------------------------------------
    def query(self, limit: int = 100, verdict: str | None = None) -> dict:
        con = self._plain_connect(self._db_path)
        con.row_factory = sqlite3.Row
        try:
            where, params = "", []
            if verdict and verdict != "all":
                where = "WHERE verdict = ?"
                params.append(verdict)
            rows = con.execute(
                f"SELECT * FROM shadow_entries {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
            summary = con.execute(
                "SELECT verdict, COUNT(*) n FROM shadow_entries WHERE resolved=1 GROUP BY verdict"
            ).fetchall()
            missed = con.execute(
                "SELECT * FROM shadow_entries WHERE verdict='MISSED_WINNER' ORDER BY fav_pct_60m DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return {
                "entries": [dict(r) for r in rows],
                "summary": {r["verdict"]: r["n"] for r in summary},
                "missed_winners": [dict(r) for r in missed],
            }
        finally:
            con.close()


shadow_ledger = ShadowLedger()


async def resolver_loop(data_manager, interval_sec: int = 60) -> None:
    """Periodic resolver task. Spawn from lifespan; cancel on shutdown."""
    while True:
        try:
            await asyncio.sleep(interval_sec)
            n = await shadow_ledger.resolve_pending(data_manager)
            if n:
                applog.info("shadow", f"resolved {n} shadow entries")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            applog.warning("shadow", f"resolver cycle failed: {e}")
