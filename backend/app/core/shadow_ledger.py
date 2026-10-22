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
import contextvars
import math
import os
import queue
import re
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
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
    signal_timestamp_utc TEXT,
    decision_bar_timestamp_utc TEXT,
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
CREATE INDEX IF NOT EXISTS idx_shadow_verdict ON shadow_entries(verdict);
CREATE INDEX IF NOT EXISTS idx_shadow_gate ON shadow_entries(gate);
CREATE INDEX IF NOT EXISTS idx_shadow_candidate_gate ON shadow_entries(candidate_id, gate, symbol);
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


def _normalise_timestamp(value) -> datetime | None:  # noqa: ANN001
    """Convert datetime-like values to timezone-aware UTC without raising."""
    try:
        if isinstance(value, datetime):
            converted = value
        else:
            to_pydatetime = getattr(value, "to_pydatetime", None)
            converted = to_pydatetime() if callable(to_pydatetime) else None
        if not isinstance(converted, datetime):
            return None
        if converted.tzinfo is None:
            converted = converted.replace(tzinfo=UTC)
        return converted.astimezone(UTC)
    except Exception:
        return None


def _first_timestamp(*values) -> datetime | None:  # noqa: ANN001
    for value in values:
        if value is None:
            continue
        timestamp = _normalise_timestamp(value)
        if timestamp is not None:
            return timestamp
    return None


_CONTEXT_DISABLED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "shadow_ledger_context_disabled", default=False
)


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
        # will never have). Replays use a context-local suppression scope.
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
                columns = {
                    row[1]
                    for row in con.execute("PRAGMA table_info(shadow_entries)").fetchall()
                }
                for column in ("signal_timestamp_utc", "decision_bar_timestamp_utc"):
                    if column not in columns:
                        con.execute(f"ALTER TABLE shadow_entries ADD COLUMN {column} TEXT")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_shadow_decision_bar "
                    "ON shadow_entries(decision_bar_timestamp_utc)"
                )
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
        decision_bar_timestamp_utc: datetime | None = None,
        candidate_id: str | None = None,
        extra_reason: str = "",
    ) -> None:
        """Capture one rejected entry signal. Never raises."""
        if not self.enabled or _CONTEXT_DISABLED.get():
            return
        try:
            now = datetime.now(UTC)
            reason_full = f"{reason}{(' | ' + extra_reason) if extra_reason else ''}"
            decision_ts = _first_timestamp(
                decision_bar_timestamp_utc,
                getattr(sig, "decision_bar_timestamp_utc", None),
            )
            signal_ts = _first_timestamp(
                getattr(sig, "signal_timestamp_utc", None),
                getattr(sig, "timestamp", None),
                getattr(sig, "ts", None),
            )

            def _finite(value):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return None
                return number if math.isfinite(number) else None

            row = {
                "created_at": now.isoformat(),
                "signal_timestamp_utc": signal_ts.isoformat() if signal_ts else None,
                "decision_bar_timestamp_utc": decision_ts.isoformat() if decision_ts else None,
                "session_date": (decision_ts or signal_ts or now).date().isoformat(),
                "symbol": symbol.upper(),
                "strategy": strategy,
                "action": action,
                "signal_price": _finite(price),
                "stop_loss": _finite(stop_loss),
                "take_profit": _finite(take_profit),
                "atr14": _finite(atr14),
                "confidence": _finite(confidence),
                "ml_probability": _finite(getattr(sig, "ml_probability", None)),
                "meta_prob": _parse_meta_prob(reason_full),
                "trade_score": _parse_trade_score(reason_full),
                "candidate_id": candidate_id or getattr(sig, "candidate_id", None),
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

    @contextmanager
    def disabled(self):
        """Suppress this context's records without mutating process-global state.

        Continuous replays run beside the live resolver and, in some deployments,
        beside a live engine tick. A context-local switch isolates replay writes
        without silencing unrelated live tasks.
        """
        token = _CONTEXT_DISABLED.set(True)
        try:
            yield
        finally:
            _CONTEXT_DISABLED.reset(token)

    def flush(self, timeout_sec: float = 10.0) -> bool:
        """Wait until all queued rows have been committed by the writer."""
        timeout = max(0.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    applog.warning(
                        "shadow",
                        f"writer flush timed out with {self._queue.unfinished_tasks} rows pending",
                    )
                    return False
                self._queue.all_tasks_done.wait(timeout=remaining)
        return True

    def _start_writer(self) -> None:
        def _writer_loop() -> None:
            _dbg("writer thread started")
            con: sqlite3.Connection | None = None
            cols = ["created_at", "signal_timestamp_utc", "decision_bar_timestamp_utc", "session_date", "symbol", "strategy", "action", "signal_price", "stop_loss", "take_profit", "atr14", "confidence", "ml_probability", "meta_prob", "trade_score", "candidate_id", "gate", "reason"]
            sql = (
                f"INSERT INTO shadow_entries ({', '.join(cols)}) "
                f"SELECT {', '.join(':' + c for c in cols)} "
                "WHERE :candidate_id IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM shadow_entries "
                "WHERE candidate_id = :candidate_id AND gate = :gate AND symbol = :symbol"
                ")"
            )
            try:
                while not self._stop.is_set():
                    if con is None:
                        try:
                            con = self._connect()
                            _dbg("writer connected")
                        except Exception as ex:
                            _dbg(f"writer connect FAILED: {ex!r}")
                            self._stop.wait(1.0)
                            continue

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
                    # Retry until SQLite accepts the batch. Dropping a batch after
                    # a fixed retry count silently loses rejection forensics.
                    attempt = 0
                    committed = False
                    while not committed and not self._stop.is_set():
                        try:
                            con.executemany(sql, batch)
                            con.commit()
                            committed = True
                            _dbg(f"committed {len(batch)} rows (attempt {attempt})")
                        except sqlite3.OperationalError as e:
                            with suppress(sqlite3.Error):
                                con.rollback()
                            attempt += 1
                            if attempt == 1 or attempt % 5 == 0:
                                applog.warning("shadow", f"write batch waiting on SQLite ({attempt} attempts): {e}")
                            self._stop.wait(min(5.0, 0.2 * attempt))
                        except Exception as e:  # noqa: BLE001
                            with suppress(sqlite3.Error):
                                con.rollback()
                            applog.warning("shadow", f"writer flush failed: {e}")
                            committed = True  # Invalid rows cannot be retried as a batch.
                            break
                    for _ in batch:
                        self._queue.task_done()
            finally:
                if con is not None:
                    with suppress(sqlite3.Error):
                        con.close()

        self._thread = threading.Thread(target=_writer_loop, name="shadow-ledger", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    async def resolve_pending(self, data_manager, max_rows: int = 300) -> int:
        """Update forward-return fields for unresolved entries. Read-only w.r.t. prod."""
        if not self.enabled or _CONTEXT_DISABLED.get():
            return 0

        updated = 0

        def _fetch(symbol: str, target: datetime):
            """Return bars, an empty frame for confirmed no-bars, or None for unavailable data."""
            try:
                if data_manager is None:
                    return None
                df = data_manager.frame(symbol, "1Min", limit=600)
                if df is not None and not getattr(df, "empty", True):
                    idx = df.index
                    if getattr(idx, "tz", None) is None:
                        idx = idx.tz_localize(UTC)
                    if len(idx) and idx.min() <= target <= idx.max():
                        horizon = target + timedelta(minutes=60)
                        # A rolling frame may still contain the decision bar
                        # while ending before the full outcome horizon. Let
                        # the cache/history path fill that older window.
                        if idx.max() >= horizon or datetime.now(UTC) < horizon:
                            return df
                        if not callable(getattr(data_manager, "history", None)):
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
                    history_df = _aio.run(_coro)
                    if history_df is not None and not getattr(history_df, "empty", True):
                        return history_df
                    # A completed history request with no bars is a real
                    # no-bars result; transport/provider exceptions are handled
                    # separately below and remain retryable.
                    return history_df
                except Exception:
                    # RESOLVER REPAIR 2026-10-16: history() enforces session
                    # coverage validation, which rejects partial/intraday
                    # sessions. Forensics is read-only: bypass the
                    # trading-path coverage gate and read raw cached bars
                    # directly. The resolver still requires a complete forward
                    # horizon before assigning a verdict.
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
                    # ``None`` means the data source could not answer. Do not
                    # age this row into NO_BARS: that verdict is reserved for a
                    # completed lookup that genuinely returned no usable bars.
                    return None
            except Exception:
                return None

        con = await asyncio.to_thread(self._connect)
        errors = 0
        skipped = 0
        try:
            rows = await asyncio.to_thread(
                con.execute,
                "SELECT id, created_at, decision_bar_timestamp_utc, symbol, action, "
                "signal_price, stop_loss, atr14 "
                "FROM shadow_entries WHERE resolved = 0 ORDER BY id LIMIT ?",
                (max_rows,),
            )
            pending = rows.fetchall()
            for row_id, created_at, decision_bar_timestamp_utc, symbol, action, entry_px, stop_px, atr in pending:
                try:
                    created_dt = datetime.fromisoformat(created_at)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=UTC)
                    decision_dt = created_dt
                    if decision_bar_timestamp_utc:
                        try:
                            decision_dt = datetime.fromisoformat(decision_bar_timestamp_utc)
                            if decision_dt.tzinfo is None:
                                decision_dt = decision_dt.replace(tzinfo=UTC)
                        except (TypeError, ValueError):
                            decision_dt = created_dt
                    age = datetime.now(UTC) - decision_dt
                    target = decision_dt.astimezone(UTC).replace(second=0, microsecond=0)
                    df = await asyncio.to_thread(_fetch, symbol, target)
                    if df is None:
                        # Provider/cache unavailable: retry on a later cycle.
                        # It is not evidence that the rejected candidate had no
                        # forward bars.
                        skipped += 1
                        continue
                    if getattr(df, "empty", True):
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
                    assert df is not None
                    idx = df.index
                    # Normalize the bar index to tz-aware UTC regardless of what
                    # the provider returns (naive UTC, aware, etc.). A mismatch
                    # here silently skipped every row (2026-08-24 finding).
                    if getattr(idx, "tz", None) is None:
                        idx = idx.tz_localize(UTC)
                    idx = idx.tz_convert(UTC)
                    # Positional lookup: get_loc breaks on duplicate timestamps.
                    start_loc = int(idx.searchsorted(target, side="left"))
                    horizon = target + timedelta(minutes=60)
                    horizon_loc = int(idx.searchsorted(horizon, side="left"))
                    if start_loc >= len(idx) or horizon_loc >= len(idx):
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
                    # Do not label a partial observation window as a real
                    # 60-minute outcome by repeating the last available price.
                    # A complete horizon is required; stale partial data ages
                    # out as NO_BARS instead.
                    if age < timedelta(minutes=60):
                        skipped += 1
                        continue
                    window = df.iloc[start_loc : horizon_loc + 1]
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
                    closes = [float(x) for x in window["close"]]
                    highs = [float(x) for x in window["high"]]
                    lows = [float(x) for x in window["low"]]
                    if not all(math.isfinite(value) for values in (closes, highs, lows) for value in values):
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
                    try:
                        entry = float(entry_px) if entry_px is not None else closes[0]
                    except (TypeError, ValueError):
                        entry = closes[0]
                    if not math.isfinite(entry) or entry <= 0:
                        entry = closes[0]
                    if not math.isfinite(entry) or entry <= 0:
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

                    def px_at(
                        minutes: int,
                        _target: datetime = target,
                        _idx=idx,
                        _start_loc: int = start_loc,
                        _closes: list[float] = closes,
                    ) -> float:
                        desired = _target + timedelta(minutes=minutes)
                        position = int(_idx.searchsorted(desired, side="left"))
                        relative = min(max(position - _start_loc, 0), len(_closes) - 1)
                        return _closes[relative]

                    p5, p15, p30, p60 = px_at(5), px_at(15), px_at(30), px_at(60)
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
                        try:
                            risk = abs(entry - float(stop_px))
                            if not math.isfinite(risk) or risk <= 0:
                                risk = abs(float(atr))
                            if math.isfinite(risk) and risk > 0:
                                r_fav = (p60 - entry) * sign / risk
                        except (TypeError, ValueError):
                            r_fav = None
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
            # Preserve updates already issued if the resolver task is cancelled
            # during application shutdown. Re-resolution is idempotent, but
            # losing a completed cycle makes the durable verdict lag forever.
            if con.in_transaction:
                with suppress(sqlite3.Error):
                    con.commit()
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
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 100
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
            summary_where = "WHERE resolved=1"
            summary_params: list[str] = []
            if where:
                summary_where += " AND verdict = ?"
                summary_params.append(verdict)
            summary = con.execute(
                f"SELECT verdict, COUNT(*) n FROM shadow_entries {summary_where} GROUP BY verdict",
                summary_params,
            ).fetchall()
            gate_where = f" {where}" if where else ""
            gate_summary = con.execute(
                "SELECT gate, COUNT(*) total, "
                "SUM(CASE WHEN verdict='MISSED_WINNER' THEN 1 ELSE 0 END) missed, "
                "SUM(CASE WHEN verdict='AVOIDED_LOSER' THEN 1 ELSE 0 END) avoided, "
                "AVG(CASE WHEN resolved=1 AND verdict NOT IN ('NO_BARS') "
                "AND fav_pct_60m IS NOT NULL THEN fav_pct_60m END) avg_forward_move "
                f"FROM shadow_entries{gate_where} GROUP BY gate ORDER BY total DESC",
                params,
            ).fetchall()
            missed_where = "WHERE verdict='MISSED_WINNER'"
            missed_params: list[str] = []
            if where:
                missed_where += " AND verdict = ?"
                missed_params.append(verdict)
            missed = con.execute(
                f"SELECT * FROM shadow_entries {missed_where} ORDER BY fav_pct_60m DESC",
                missed_params,
            ).fetchall()
            return {
                "entries": [dict(r) for r in rows],
                "summary": {r["verdict"]: r["n"] for r in summary},
                "gate_summary": [dict(r) for r in gate_summary],
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
            current_data_manager = data_manager() if callable(data_manager) else data_manager
            n = await shadow_ledger.resolve_pending(current_data_manager)
            if n:
                applog.info("shadow", f"resolved {n} shadow entries")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            applog.warning("shadow", f"resolver cycle failed: {e}")
