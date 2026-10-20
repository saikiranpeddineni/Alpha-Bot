"""Read-only retrospective audit of the PIS EOD-peak criterion.

This script reconstructs completed trades against cached 1-minute bars.  It is
deliberately a forensic tool: ``eod_peak_r`` uses bars that may occur after the
recorded exit and is therefore hindsight-only.  EOD peak values and the
classifications in this module must never feed production entry, exit, sizing,
or protection decisions.

The production databases are opened with SQLite's read-only URI mode and
``PRAGMA query_only``.  The only file this script can write is the optional
report passed through ``--out``.

Definitions
-----------
* in-trade peak R: the best favorable 1-minute high/low from the entry minute
  through the minute before the recorded exit, divided by initial risk;
* EOD peak R: the best favorable high/low from the entry minute through the
  configured session close, including bars after the recorded exit;
* realized R: the price-based realized move divided by initial risk;
* capture ratio: realized R / in-trade peak R when the in-trade peak is
  positive;
* missed R: ``max(0, EOD peak R - realized R)``; this is an opportunity-cost
  measure, not a counterfactual production result;
* post-exit continuation: the best favorable movement after the exit, measured
  from the exit price in the original risk unit.

The implementation uses only Python's standard library so it can run against a
copied database on a machine without the application environment installed.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime, time
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


DEFAULT_SESSION_CLOSE_UTC = time(20, 0, 0)
MATERIAL_CONTINUATION_R = 1.0
MEANINGFUL_PEAK_R = 0.5
PROPER_CAPTURE_RATIO = 0.75


def _default_database(filename: str) -> Path:
    """Return the canonical database path, with a legacy backend fallback."""

    repo_root = Path(__file__).resolve().parents[2]
    candidates = (repo_root / "data" / filename, repo_root / "backend" / "data" / filename)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Keep the user-facing default stable even before the database is created.
    return candidates[0]


DEFAULT_TRADES_DB = _default_database("alphadesk.db")
DEFAULT_BARS_DB = _default_database("bars_cache.db")


def _finite_float(value: Any) -> float | None:
    """Convert a value to a finite float, returning ``None`` for bad input."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _json_float(value: float | None) -> float | None:
    """Round report numbers while keeping missing/invalid values as JSON null."""

    if value is None or not math.isfinite(value):
        return None
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded


def _pick(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value from a mapping."""

    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse common SQLite/JSON timestamps into timezone-aware UTC datetimes.

    Naive ISO values are treated as UTC because the trading ledger and bar cache
    are UTC data sources.  Numeric values are accepted as Unix seconds, with a
    millisecond fallback for exported bar data.
    """

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _finite_float(value)
        if number is None:
            return None
        if abs(number) > 100_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_session_close_utc(value: str | time | datetime) -> time:
    """Parse ``HH:MM[:SS]`` or an ISO timestamp into a UTC clock time."""

    if isinstance(value, time):
        return value.replace(tzinfo=None, microsecond=0)
    if isinstance(value, datetime):
        return value.astimezone(UTC).time().replace(tzinfo=None, microsecond=0)

    raw = str(value).strip()
    if not raw:
        raise ValueError("session close must be HH:MM, HH:MM:SS, or an ISO timestamp")
    parsed_datetime = parse_timestamp(raw)
    if parsed_datetime is not None and ("T" in raw or " " in raw or len(raw) > 8):
        return parsed_datetime.time().replace(tzinfo=None, microsecond=0)
    if raw.endswith(("Z", "z")):
        raw = raw[:-1]
    parts = raw.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid UTC session close {value!r}; use HH:MM or HH:MM:SS")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        parsed_time = time(hour, minute, second)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid UTC session close {value!r}; use HH:MM or HH:MM:SS") from exc
    return parsed_time


def _session_close_datetime(session_date: date | None, close_time: time) -> datetime | None:
    if session_date is None:
        return None
    return datetime.combine(session_date, close_time, tzinfo=UTC)


def _canonical_side(raw_side: Any, raw_qty: Any = None) -> tuple[str, float]:
    """Return a stable long/short side and its signed direction."""

    if isinstance(raw_side, (int, float)) and not isinstance(raw_side, bool):
        return ("short", -1.0) if float(raw_side) < 0 else ("long", 1.0)
    text = str(raw_side or "").strip().lower()
    if text in {"short", "sell", "sold", "sell_short", "-1", "-"}:
        return "short", -1.0
    if not text:
        qty = _finite_float(raw_qty)
        if qty is not None and qty < 0:
            return "short", -1.0
    return "long", 1.0


def _floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _favorable_r(price: float, entry_price: float, risk: float, direction: float) -> float:
    return (price - entry_price) / risk * direction


def _bar_extreme(bar: Mapping[str, Any], direction: float) -> float | None:
    """Use the directional high/low, with close as a malformed-bar fallback."""

    key = "high" if direction > 0 else "low"
    extreme = _finite_float(bar.get(key))
    if extreme is not None and extreme > 0:
        return extreme
    close = _finite_float(bar.get("close"))
    return close if close is not None and close > 0 else None


def _peak_r(
    bars: Sequence[Mapping[str, Any]],
    *,
    entry_price: float,
    risk: float,
    direction: float,
) -> tuple[float | None, datetime | None]:
    best: float | None = None
    best_at: datetime | None = None
    for bar in bars:
        extreme = _bar_extreme(bar, direction)
        if extreme is None:
            continue
        value = _favorable_r(extreme, entry_price, risk, direction)
        if best is None or value > best:
            best = value
            best_at = bar.get("timestamp")
    return best, best_at


def _post_exit_continuation(
    bars: Sequence[Mapping[str, Any]],
    *,
    exit_price: float,
    risk: float,
    direction: float,
) -> tuple[float | None, datetime | None]:
    """Return the best non-negative post-exit move in R from the exit price."""

    best: float | None = None
    best_at: datetime | None = None
    for bar in bars:
        extreme = _bar_extreme(bar, direction)
        if extreme is None:
            continue
        favorable_move = (extreme - exit_price) * direction / risk
        value = max(0.0, favorable_move)
        if best is None or value > best:
            best = value
            best_at = bar.get("timestamp")
    return best, best_at


def _is_safety_stop(exit_reason: Any, *, exit_price: float | None, stop_price: float | None, direction: float) -> bool:
    reason = str(exit_reason or "").strip().lower().replace("_", " ")
    safety_tokens = (
        "stop loss",
        "stoploss",
        "protection breach",
        "protective stop",
        "broker stop",
        "safety stop",
        "emergency",
        "kill switch",
    )
    if any(token in reason for token in safety_tokens):
        return True
    if exit_price is None or stop_price is None or stop_price <= 0:
        return False
    tolerance = max(1e-6, stop_price * 1e-5)
    return (direction > 0 and exit_price <= stop_price + tolerance) or (
        direction < 0 and exit_price >= stop_price - tolerance
    )


def classify_outcome(
    *,
    risk_valid: bool,
    bars_available: bool,
    in_trade_peak_r: float | None,
    realized_r: float | None,
    capture_ratio: float | None,
    missed_r: float | None,
    post_exit_continuation_r: float | None,
    exit_reason: Any = "",
    exit_price: float | None = None,
    stop_price: float | None = None,
    direction: float = 1.0,
) -> tuple[str, str, str]:
    """Classify a row and return ``(human_label, code, explanation)``.

    The thresholds are intentionally conservative and are audit labels, not
    PIS reason codes.  A protective stop is classified first; a discretionary
    exit with a small/negative peak followed by >= 1R continuation is
    ``premature``; meaningful excursions with a weak capture are ``low
    harvest``.  All remaining usable rows are ``proper peak capture`` (the
    contained-loss case included).
    """

    if not risk_valid:
        return "invalid risk", "invalid_risk", "Initial risk is missing, zero, negative, or non-finite."
    if not bars_available:
        return "no bars", "no_bars", "No usable 1-minute bars covered the requested reconstruction window."
    if _is_safety_stop(exit_reason, exit_price=exit_price, stop_price=stop_price, direction=direction):
        return "safety-stop", "safety_stop", "Exit reason or exit price indicates a protective/safety stop."
    if in_trade_peak_r is None or realized_r is None:
        return "insufficient data", "insufficient_data", "Usable bars or price fields were insufficient for classification."

    continuation = post_exit_continuation_r or 0.0
    if continuation >= MATERIAL_CONTINUATION_R and (
        in_trade_peak_r < MEANINGFUL_PEAK_R or realized_r <= 0.0
    ):
        return (
            "premature",
            "premature",
            "Discretionary exit was low/negative R and was followed by material favorable continuation.",
        )

    if in_trade_peak_r >= MEANINGFUL_PEAK_R and (
        (capture_ratio is not None and capture_ratio < PROPER_CAPTURE_RATIO)
        or (missed_r is not None and missed_r >= 1.0)
        or continuation >= MATERIAL_CONTINUATION_R
    ):
        return (
            "low harvest",
            "low_harvest",
            "A meaningful excursion was not mostly retained, or the EOD opportunity cost was material.",
        )

    return "proper peak capture", "proper_peak_capture", "Contained the move or retained most of the in-trade peak."


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except sqlite3.Error:
        return set()


def connect_read_only(path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite file without permitting writes or file creation."""

    db_path = Path(path).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _load_trade_records(
    conn: sqlite3.Connection,
    *,
    target_date: date | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    columns = _table_columns(conn, "trades")
    if not columns:
        return [], ["trades_table_missing"]

    selected = [name for name in ("id", "exit_at", "symbol", "pnl", "mode", "payload") if name in columns]
    if "payload" not in selected:
        warnings.append("trades_payload_column_missing")
    if not selected:
        return [], ["trades_columns_missing"]
    order_column = "exit_at" if "exit_at" in columns else ("id" if "id" in columns else selected[0])
    try:
        rows = conn.execute(
            f'SELECT {", ".join(selected)} FROM "trades" ORDER BY "{order_column}" ASC'
        ).fetchall()
    except sqlite3.Error as exc:
        return [], [f"trades_query_failed:{exc}"]

    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        source = dict(row)
        raw_payload = source.get("payload")
        payload: dict[str, Any]
        if isinstance(raw_payload, str):
            try:
                decoded = json.loads(raw_payload)
                payload = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
                warnings.append(f"invalid_trade_payload:{source.get('id', index)}")
        else:
            payload = {}

        # The ledger columns remain the fallback for old payloads.  Payload
        # values win when present because they contain the full TradeRecord.
        record = dict(payload)
        if _pick(record, "id", "trade_id") is None:
            record["id"] = source.get("id") or f"row-{index}"
        if _pick(record, "exit_at") is None:
            record["exit_at"] = source.get("exit_at")
        if _pick(record, "symbol") is None:
            record["symbol"] = source.get("symbol") or ""
        if _pick(record, "pnl") is None:
            record["pnl"] = source.get("pnl")
        if _pick(record, "mode") is None:
            record["mode"] = source.get("mode")

        exit_dt = parse_timestamp(_pick(record, "exit_at", "exit_fill_timestamp_utc"))
        if target_date is not None:
            if exit_dt is not None:
                if exit_dt.date() != target_date:
                    continue
            else:
                # Keep malformed rows when their entry date identifies the
                # session.  If both timestamps are malformed, retain the row
                # as an explicit invalid-timestamp audit result rather than
                # silently hiding it from a date-scoped report.
                entry_dt = parse_timestamp(
                    _pick(record, "entry_at", "entry_fill_timestamp_utc", "opened_at", "entry_timestamp")
                )
                if entry_dt is not None and entry_dt.date() != target_date:
                    continue
        records.append(record)
    return records, warnings


def _load_bars(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load 1-minute bars for a symbol, supporting seconds or millisecond ts."""

    columns = _table_columns(conn, "bars")
    if not columns:
        return [], ["bars_table_missing"]
    required = {"symbol", "ts"}
    if not required.issubset(columns):
        return [], ["bars_required_columns_missing"]

    selected = [
        f'"{name}" AS "{name}"' if name in columns else f"NULL AS {name}"
        for name in ("ts", "open", "high", "low", "close", "volume")
    ]
    tf_clause = " AND lower(tf) = '1min'" if "tf" in columns else ""
    start_epoch = int(_floor_minute(start).timestamp())
    end_epoch = int(end.timestamp())

    def query(low: int, high: int) -> list[sqlite3.Row]:
        sql = (
            f'SELECT {", ".join(selected)} FROM "bars" '
            f"WHERE symbol = ?{tf_clause} AND ts BETWEEN ? AND ? ORDER BY ts ASC"
        )
        return conn.execute(sql, (symbol, low, high)).fetchall()

    try:
        rows = query(start_epoch, end_epoch)
        timestamp_divisor = 1.0
        # Some exports retain Unix milliseconds.  A second read is only made
        # when the seconds query is empty and never mutates the source DB.
        if not rows:
            rows = query(start_epoch * 1000, end_epoch * 1000)
            timestamp_divisor = 1000.0 if rows else 1.0
        # A few exported fixtures use timezone-aware ISO text in ``ts`` rather
        # than Unix seconds.  Let SQLite return that symbol's rows and apply
        # the timezone-aware range in Python.  The production cache remains on
        # the indexed numeric path above.
        if not rows:
            text_sql = (
                f'SELECT {", ".join(selected)} FROM "bars" '
                f"WHERE symbol = ?{tf_clause} ORDER BY ts ASC"
            )
            rows = conn.execute(text_sql, (symbol,)).fetchall()
    except sqlite3.Error as exc:
        return [], [f"bars_query_failed:{exc}"]

    bars: list[dict[str, Any]] = []
    for row in rows:
        source = dict(row)
        raw_ts = _finite_float(source.get("ts"))
        if raw_ts is not None and timestamp_divisor != 1.0:
            raw_ts /= timestamp_divisor
        timestamp = parse_timestamp(raw_ts if raw_ts is not None else source.get("ts"))
        if timestamp is not None and not (start <= timestamp <= end):
            continue
        if timestamp is None:
            continue
        source["timestamp"] = timestamp
        bars.append(source)
    return bars, []


def _bar_gap_count(bars: Sequence[Mapping[str, Any]]) -> int:
    """Count short missing-bar gaps without treating overnight gaps as errors."""

    timestamps = [bar.get("timestamp") for bar in bars if isinstance(bar.get("timestamp"), datetime)]
    timestamps.sort()
    missing = 0
    for previous, current in zip(timestamps, timestamps[1:]):
        minutes = int((current - previous).total_seconds() // 60)
        # A normal US session can contain an occasional missing print.  Do not
        # call the overnight/cache gap between sessions a missing intraday bar.
        if 1 < minutes <= 30:
            missing += minutes - 1
    return missing


def audit_trade(
    trade: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    target_date: date | None = None,
    session_close_utc: time = DEFAULT_SESSION_CLOSE_UTC,
) -> dict[str, Any]:
    """Audit one normalized trade against already-loaded bars.

    This pure-ish function is also useful to focused tests: it never writes to
    a database and accepts a list of bar dictionaries.
    """

    symbol = str(_pick(trade, "symbol") or "").strip().upper()
    side, direction = _canonical_side(_pick(trade, "side", "direction"), _pick(trade, "qty", "quantity"))
    entry_raw = _pick(trade, "entry_at", "entry_fill_timestamp_utc", "opened_at", "entry_timestamp")
    exit_raw = _pick(trade, "exit_at", "exit_fill_timestamp_utc", "closed_at", "exit_timestamp")
    entry_dt = parse_timestamp(entry_raw)
    exit_dt = parse_timestamp(exit_raw)
    entry_price = _finite_float(_pick(trade, "entry_price", "avg_entry_price", "fill_entry_price"))
    exit_price = _finite_float(_pick(trade, "exit_price", "avg_exit_price", "fill_exit_price"))
    qty = _finite_float(_pick(trade, "qty", "quantity", "filled_qty"))
    pnl = _finite_float(_pick(trade, "pnl", "realized_pnl"))
    initial_stop = _finite_float(_pick(trade, "initial_stop", "stop_loss", "initial_stop_loss"))
    explicit_risk_raw = _pick(trade, "initial_risk", "risk_per_share")
    explicit_risk = _finite_float(explicit_risk_raw) if explicit_risk_raw is not None else None

    risk_reason: str | None = None
    if explicit_risk_raw is not None:
        risk = explicit_risk if explicit_risk is not None and explicit_risk > 0 else None
        if risk is None:
            risk_reason = "recorded_initial_risk_invalid"
    elif entry_price is not None and initial_stop is not None and entry_price > 0 and initial_stop > 0:
        risk = abs(entry_price - initial_stop)
        if risk <= 0 or not math.isfinite(risk):
            risk = None
            risk_reason = "entry_and_initial_stop_have_zero_distance"
    else:
        risk = None
        risk_reason = "initial_risk_and_initial_stop_missing_or_invalid"
    if entry_price is None or entry_price <= 0:
        risk = None
        risk_reason = risk_reason or "entry_price_invalid"

    exit_reason = _pick(trade, "exit_reason", "reason") or ""
    session_date = target_date or (exit_dt.date() if exit_dt else (entry_dt.date() if entry_dt else None))
    close_dt = _session_close_datetime(session_date, session_close_utc)
    carryover = bool(entry_dt and session_date and entry_dt.date() < session_date)

    data_issues: list[str] = []
    if risk_reason:
        data_issues.append(risk_reason)
    if entry_dt is None:
        data_issues.append("entry_timestamp_invalid")
    if exit_dt is None:
        data_issues.append("exit_timestamp_invalid")
    if entry_dt and exit_dt and exit_dt < entry_dt:
        data_issues.append("exit_before_entry")
    if close_dt and entry_dt and close_dt < _floor_minute(entry_dt):
        data_issues.append("session_close_before_entry")

    normalized_bars: list[dict[str, Any]] = []
    for raw_bar in bars:
        normalized_bar = dict(raw_bar)
        timestamp = normalized_bar.get("timestamp")
        timestamp = parse_timestamp(timestamp) if timestamp is not None else parse_timestamp(normalized_bar.get("ts"))
        if timestamp is not None:
            normalized_bar["timestamp"] = timestamp
            normalized_bars.append(normalized_bar)
    usable_bars = normalized_bars
    if entry_dt and close_dt and close_dt >= _floor_minute(entry_dt):
        window_bars = [
            bar
            for bar in usable_bars
            if _floor_minute(entry_dt) <= bar["timestamp"] <= close_dt
        ]
    else:
        window_bars = []
    window_bars.sort(key=lambda bar: bar["timestamp"])
    if not window_bars:
        data_issues.append("no_bars")
    missing_bar_count = _bar_gap_count(window_bars)
    if missing_bar_count:
        data_issues.append("missing_bars")

    # A trade timestamp can include seconds while bars are minute stamped.  To
    # honor "through the minute before exit", exclude the whole minute bearing
    # the exit timestamp from the in-trade peak and start continuation on the
    # following minute.
    exit_bar_start = _floor_minute(exit_dt) if exit_dt is not None else None
    in_trade_bars = [
        bar
        for bar in window_bars
        if exit_bar_start is not None and bar["timestamp"] < exit_bar_start
    ]
    post_exit_bars = [
        bar
        for bar in window_bars
        if exit_bar_start is not None and bar["timestamp"] > exit_bar_start
    ]

    in_trade_peak_r: float | None = None
    in_trade_peak_at: datetime | None = None
    eod_peak_r: float | None = None
    eod_peak_at: datetime | None = None
    realized_r: float | None = None
    capture_ratio: float | None = None
    missed_r: float | None = None
    harvest_gap_r: float | None = None
    post_exit_continuation_r: float | None = None
    post_exit_peak_at: datetime | None = None
    if risk is not None and entry_price is not None:
        in_trade_peak_r, in_trade_peak_at = _peak_r(
            in_trade_bars, entry_price=entry_price, risk=risk, direction=direction
        )
        eod_peak_r, eod_peak_at = _peak_r(
            window_bars, entry_price=entry_price, risk=risk, direction=direction
        )
        if exit_price is not None and exit_price > 0:
            realized_r = _favorable_r(exit_price, entry_price, risk, direction)
            if in_trade_peak_r is not None and in_trade_peak_r > 0:
                capture_ratio = realized_r / in_trade_peak_r
            if eod_peak_r is not None:
                missed_r = max(0.0, eod_peak_r - realized_r)
            if in_trade_peak_r is not None:
                harvest_gap_r = in_trade_peak_r - realized_r
            if exit_dt is not None:
                post_exit_continuation_r, post_exit_peak_at = _post_exit_continuation(
                    post_exit_bars,
                    exit_price=exit_price,
                    risk=risk,
                    direction=direction,
                )

    if risk is None:
        status = "invalid_risk"
    elif entry_dt is None or exit_dt is None:
        status = "invalid_timestamp"
    elif "exit_before_entry" in data_issues or "session_close_before_entry" in data_issues:
        status = "invalid_time_window"
    elif not window_bars:
        status = "no_bars"
    elif missing_bar_count:
        status = "partial_bars"
    else:
        status = "ok"

    human_class, classification_code, classification_reason = classify_outcome(
        risk_valid=risk is not None,
        bars_available=bool(window_bars),
        in_trade_peak_r=in_trade_peak_r,
        realized_r=realized_r,
        capture_ratio=capture_ratio,
        missed_r=missed_r,
        post_exit_continuation_r=post_exit_continuation_r,
        exit_reason=exit_reason,
        exit_price=exit_price,
        stop_price=initial_stop,
        direction=direction,
    )

    if pnl is None and realized_r is not None and qty is not None and qty > 0 and risk is not None:
        pnl = realized_r * risk * abs(qty)

    return {
        "trade_id": str(_pick(trade, "id", "trade_id") or ""),
        "session_date": session_date.isoformat() if session_date else None,
        "symbol": symbol,
        "side": side,
        "direction": direction,
        "mode": _pick(trade, "mode"),
        "strategy": _pick(trade, "strategy") or "",
        "entry_at": _iso_timestamp(entry_dt) or (str(entry_raw) if entry_raw not in (None, "") else None),
        "exit_at": _iso_timestamp(exit_dt) or (str(exit_raw) if exit_raw not in (None, "") else None),
        "session_close_at": _iso_timestamp(close_dt),
        "carryover": carryover,
        "qty": _json_float(abs(qty) if qty is not None else None),
        "entry_price": _json_float(entry_price),
        "exit_price": _json_float(exit_price),
        "initial_stop": _json_float(initial_stop),
        "risk_per_share": _json_float(risk),
        "pnl": _json_float(pnl),
        "exit_reason": str(exit_reason),
        "in_trade_peak_r": _json_float(in_trade_peak_r),
        "in_trade_peak_at": _iso_timestamp(in_trade_peak_at),
        "eod_peak_r": _json_float(eod_peak_r),
        "eod_peak_at": _iso_timestamp(eod_peak_at),
        "realized_r": _json_float(realized_r),
        "capture_ratio": _json_float(capture_ratio),
        "missed_r": _json_float(missed_r),
        "harvest_gap_r": _json_float(harvest_gap_r),
        "post_exit_continuation_r": _json_float(post_exit_continuation_r),
        "post_exit_peak_at": _iso_timestamp(post_exit_peak_at),
        "bar_count": len(window_bars),
        "in_trade_bar_count": len(in_trade_bars),
        "post_exit_bar_count": len(post_exit_bars),
        "missing_bar_count": missing_bar_count,
        "status": status,
        "data_issues": list(dict.fromkeys(data_issues)),
        "classification": human_class,
        "classification_code": classification_code,
        "classification_reason": classification_reason,
        "eod_peak_hindsight_only": True,
        "production_decision_use": "never",
    }


def audit_databases(
    trades_db: str | Path = DEFAULT_TRADES_DB,
    bars_db: str | Path = DEFAULT_BARS_DB,
    *,
    audit_date: str | date | None = None,
    session_close_utc: str | time | datetime = DEFAULT_SESSION_CLOSE_UTC,
) -> dict[str, Any]:
    """Read the two SQLite databases and return a JSON-serializable report."""

    if isinstance(audit_date, str):
        try:
            target_date = date.fromisoformat(audit_date)
        except ValueError as exc:
            raise ValueError(f"invalid audit date {audit_date!r}; use YYYY-MM-DD") from exc
    elif isinstance(audit_date, datetime):
        target_date = audit_date.date()
    else:
        target_date = audit_date
    close_time = parse_session_close_utc(session_close_utc)

    warnings: list[str] = []
    with closing(connect_read_only(trades_db)) as trades_conn, closing(connect_read_only(bars_db)) as bars_conn:
        records, trade_warnings = _load_trade_records(trades_conn, target_date=target_date)
        warnings.extend(trade_warnings)
        audited: list[dict[str, Any]] = []
        for record in records:
            entry_dt = parse_timestamp(_pick(record, "entry_at", "entry_fill_timestamp_utc", "opened_at", "entry_timestamp"))
            exit_dt = parse_timestamp(_pick(record, "exit_at", "exit_fill_timestamp_utc", "closed_at", "exit_timestamp"))
            session_date = target_date or (exit_dt.date() if exit_dt else (entry_dt.date() if entry_dt else None))
            close_dt = _session_close_datetime(session_date, close_time)
            if entry_dt is not None and close_dt is not None and close_dt >= _floor_minute(entry_dt):
                bars, bar_warnings = _load_bars(
                    bars_conn,
                    symbol=str(_pick(record, "symbol") or "").strip().upper(),
                    start=entry_dt,
                    end=close_dt,
                )
                warnings.extend(f"{record.get('id', '')}:{warning}" for warning in bar_warnings)
            else:
                bars = []
                if entry_dt is None:
                    warnings.append(f"{record.get('id', '')}:entry_timestamp_invalid")
            audited.append(
                audit_trade(
                    record,
                    bars,
                    target_date=target_date,
                    session_close_utc=close_time,
                )
            )

    outcome_counts = Counter(str(row["classification_code"]) for row in audited)
    status_counts = Counter(str(row["status"]) for row in audited)
    realized_values = [row["realized_r"] for row in audited if row["realized_r"] is not None]
    missed_values = [row["missed_r"] for row in audited if row["missed_r"] is not None]
    report = {
        "tool": "audit_pis_eod_peak",
        "schema_version": 1,
        "read_only": True,
        "eod_peak_hindsight_only": True,
        "production_decision_use": "never",
        "hindsight_warning": (
            "EOD peak R includes bars after the recorded exit. It is an audit-only opportunity-cost measure "
            "and must never feed production decisions."
        ),
        "definitions": {
            "in_trade_peak_r": "Best favorable 1-minute high/low from entry minute through the minute before exit.",
            "eod_peak_r": "Best favorable 1-minute high/low from entry through session close, including post-exit bars.",
            "realized_r": "Price-based realized move divided by recorded initial risk.",
            "capture_ratio": "realized_r / in_trade_peak_r when the in-trade peak is positive.",
            "missed_r": "max(0, eod_peak_r - realized_r), an opportunity-cost measure.",
            "post_exit_continuation_r": "Best favorable movement after exit from the exit price in original R units.",
        },
        "classification_policy": {
            "meaningful_peak_r": MEANINGFUL_PEAK_R,
            "material_continuation_r": MATERIAL_CONTINUATION_R,
            "proper_capture_ratio": PROPER_CAPTURE_RATIO,
            "labels": ["proper peak capture", "low harvest", "premature", "safety-stop"],
        },
        "trades_db": str(Path(trades_db).expanduser().resolve()),
        "bars_db": str(Path(bars_db).expanduser().resolve()),
        "date": target_date.isoformat() if target_date else None,
        "session_close_utc": close_time.isoformat(),
        "trade_count": len(audited),
        "status_counts": dict(sorted(status_counts.items())),
        "classification_counts": dict(sorted(outcome_counts.items())),
        "mean_realized_r": _json_float(sum(realized_values) / len(realized_values) if realized_values else None),
        "mean_missed_r": _json_float(sum(missed_values) / len(missed_values) if missed_values else None),
        "warnings": list(dict.fromkeys(warnings)),
        "trades": audited,
    }
    return report


def _csv_fieldnames(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    preferred = [
        "trade_id",
        "session_date",
        "symbol",
        "side",
        "carryover",
        "entry_at",
        "exit_at",
        "entry_price",
        "exit_price",
        "risk_per_share",
        "in_trade_peak_r",
        "eod_peak_r",
        "realized_r",
        "capture_ratio",
        "missed_r",
        "harvest_gap_r",
        "post_exit_continuation_r",
        "exit_reason",
        "classification",
        "classification_code",
        "status",
        "bar_count",
        "data_issues",
    ]
    discovered = {key for row in rows for key in row}
    return preferred + sorted(discovered - set(preferred))


def write_report(report: Mapping[str, Any], out_path: str | Path) -> None:
    """Write JSON by default, or one flat per-trade CSV for a ``.csv`` path."""

    destination = Path(out_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".csv":
        rows = list(report.get("trades", []))
        fieldnames = _csv_fieldnames(rows)
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                flattened = dict(row)
                flattened["data_issues"] = ";".join(str(item) for item in row.get("data_issues", []))
                writer.writerow(flattened)
    else:
        destination.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only PIS EOD-peak retrospective audit. EOD peak is hindsight-only "
            "and never feeds production decisions."
        )
    )
    parser.add_argument("--trades-db", type=Path, default=DEFAULT_TRADES_DB)
    parser.add_argument("--bars-db", type=Path, default=DEFAULT_BARS_DB)
    parser.add_argument("--date", dest="audit_date", default=None, help="session date (YYYY-MM-DD); default audits all trade exit dates")
    parser.add_argument(
        "--session-close-utc",
        default="20:00:00",
        help="UTC session close as HH:MM[:SS] or an ISO timestamp (default: 20:00:00)",
    )
    parser.add_argument("--out", type=Path, default=None, help="optional JSON path, or .csv path for flat trade rows")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = audit_databases(
            args.trades_db,
            args.bars_db,
            audit_date=args.audit_date,
            session_close_utc=args.session_close_utc,
        )
        if args.out is not None:
            input_paths = {
                Path(args.trades_db).expanduser().resolve(),
                Path(args.bars_db).expanduser().resolve(),
            }
            if Path(args.out).expanduser().resolve() in input_paths:
                raise ValueError("--out must not overwrite an input SQLite database")
            write_report(report, args.out)
        print(json.dumps(report, indent=2, sort_keys=False))
        return 0
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        error = {
            "tool": "audit_pis_eod_peak",
            "read_only": True,
            "eod_peak_hindsight_only": True,
            "production_decision_use": "never",
            "error": str(exc),
        }
        print(json.dumps(error, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
