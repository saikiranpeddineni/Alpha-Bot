"""Focused fixtures for the read-only PIS EOD-peak audit."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
import subprocess
import sys

import pytest

from scripts.audit_pis_eod_peak import audit_databases, connect_read_only


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _create_fixture_databases(tmp_path):
    trades_db = tmp_path / "trades.db"
    bars_db = tmp_path / "bars.db"
    trades = sqlite3.connect(trades_db)
    trades.execute(
        """
        CREATE TABLE trades (
            id TEXT PRIMARY KEY,
            exit_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            pnl REAL NOT NULL,
            mode TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    bars = sqlite3.connect(bars_db)
    bars.execute(
        """
        CREATE TABLE bars (
            symbol TEXT NOT NULL,
            tf TEXT NOT NULL,
            ts INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (symbol, tf, ts)
        )
        """
    )

    def add_trade(trade_id: str, payload: dict) -> None:
        trades.execute(
            "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?)",
            (
                trade_id,
                payload["exit_at"],
                payload["symbol"],
                payload.get("pnl", 0.0),
                "paper",
                json.dumps({"id": trade_id, **payload}),
            ),
        )

    def add_bar(
        symbol: str,
        timestamp: str,
        *,
        high: float,
        low: float,
        close: float,
        iso_ts: bool = False,
    ) -> None:
        bars.execute(
            "INSERT INTO bars VALUES (?, '1Min', ?, ?, ?, ?, ?, ?)",
            (symbol, timestamp if iso_ts else _epoch(timestamp), close, high, low, close, 1_000.0),
        )

    # Carryover long: the entry is on the prior UTC date but the completed
    # trade belongs to the requested session by its exit date.
    add_trade(
        "long-1",
        {
            "symbol": "LONG",
            "side": "long",
            "qty": 10,
            "entry_at": "2026-10-16T19:59:00Z",
            "exit_at": "2026-10-17T14:03:00Z",
            "entry_price": 100.0,
            "exit_price": 108.0,
            "initial_stop": 95.0,
            "initial_risk": 5.0,
            "exit_reason": "manual close",
        },
    )
    add_bar("LONG", "2026-10-16T19:59:00Z", high=101.0, low=99.0, close=100.5)
    add_bar("LONG", "2026-10-17T14:00:00Z", high=105.0, low=99.0, close=104.0)
    add_bar("LONG", "2026-10-17T14:01:00Z", high=110.0, low=103.0, close=109.0)
    add_bar("LONG", "2026-10-17T14:02:00Z", high=108.0, low=104.0, close=107.0)
    add_bar("LONG", "2026-10-17T14:03:00Z", high=109.0, low=106.0, close=108.0)
    add_bar("LONG", "2026-10-17T14:04:00Z", high=109.0, low=107.0, close=108.5)

    add_trade(
        "short-1",
        {
            "symbol": "SHORT",
            "side": "short",
            "qty": 5,
            "entry_at": "2026-10-17T15:00:00+00:00",
            "exit_at": "2026-10-17T15:03:00+00:00",
            "entry_price": 100.0,
            "exit_price": 92.0,
            "initial_stop": 105.0,
            "initial_risk": 5.0,
            "exit_reason": "manual close",
        },
    )
    add_bar("SHORT", "2026-10-17T15:00:00Z", high=101.0, low=98.0, close=99.0, iso_ts=True)
    add_bar("SHORT", "2026-10-17T15:01:00Z", high=99.0, low=90.0, close=91.0, iso_ts=True)
    add_bar("SHORT", "2026-10-17T15:02:00Z", high=95.0, low=92.0, close=93.0, iso_ts=True)
    add_bar("SHORT", "2026-10-17T15:03:00Z", high=94.0, low=91.0, close=92.0, iso_ts=True)
    add_bar("SHORT", "2026-10-17T15:04:00Z", high=94.0, low=91.0, close=92.0, iso_ts=True)

    # Small early excursion followed by a large post-exit continuation: this
    # is the retrospective premature-exit case.
    add_trade(
        "continuation-1",
        {
            "symbol": "CONT",
            "side": "long",
            "qty": 10,
            "entry_at": "2026-10-17T16:00:00Z",
            "exit_at": "2026-10-17T16:02:00Z",
            "entry_price": 100.0,
            "exit_price": 99.0,
            "initial_stop": 95.0,
            "initial_risk": 5.0,
            "exit_reason": "THESIS_DEGRADED",
        },
    )
    add_bar("CONT", "2026-10-17T16:00:00Z", high=101.0, low=99.0, close=100.5)
    add_bar("CONT", "2026-10-17T16:01:00Z", high=101.0, low=98.0, close=99.0)
    add_bar("CONT", "2026-10-17T16:02:00Z", high=100.0, low=98.0, close=99.0)
    add_bar("CONT", "2026-10-17T16:03:00Z", high=110.0, low=99.0, close=109.0)

    add_trade(
        "missing-1",
        {
            "symbol": "MISSING",
            "side": "long",
            "qty": 1,
            "entry_at": "2026-10-17T17:00:00Z",
            "exit_at": "2026-10-17T17:05:00Z",
            "entry_price": 100.0,
            "exit_price": 101.0,
            "initial_stop": 95.0,
            "initial_risk": 5.0,
            "exit_reason": "manual close",
        },
    )

    # An explicit zero risk must remain invalid rather than being silently
    # repaired from a stop field.
    add_trade(
        "invalid-risk-1",
        {
            "symbol": "BADRISK",
            "side": "long",
            "qty": 1,
            "entry_at": "2026-10-17T18:00:00Z",
            "exit_at": "2026-10-17T18:05:00Z",
            "entry_price": 100.0,
            "exit_price": 105.0,
            "initial_stop": 95.0,
            "initial_risk": 0.0,
            "exit_reason": "stop_loss",
        },
    )
    add_bar("BADRISK", "2026-10-17T18:00:00Z", high=105.0, low=99.0, close=104.0)

    trades.commit()
    bars.commit()
    trades.close()
    bars.close()
    return trades_db, bars_db


def test_eod_peak_audit_handles_long_short_carryover_and_continuation(tmp_path):
    trades_db, bars_db = _create_fixture_databases(tmp_path)

    report = audit_databases(
        trades_db,
        bars_db,
        audit_date="2026-10-17",
        session_close_utc="20:00",
    )

    assert report["read_only"] is True
    assert report["eod_peak_hindsight_only"] is True
    assert report["production_decision_use"] == "never"
    assert report["trade_count"] == 5
    by_id = {row["trade_id"]: row for row in report["trades"]}

    long = by_id["long-1"]
    assert long["carryover"] is True
    assert long["in_trade_peak_r"] == 2.0
    assert long["eod_peak_r"] == 2.0
    assert long["realized_r"] == 1.6
    assert long["capture_ratio"] == 0.8
    assert long["missed_r"] == 0.4
    assert long["post_exit_continuation_r"] == 0.2
    assert long["classification"] == "proper peak capture"

    short = by_id["short-1"]
    assert short["side"] == "short"
    assert short["in_trade_peak_r"] == 2.0
    assert short["eod_peak_r"] == 2.0
    assert short["realized_r"] == 1.6
    assert short["post_exit_continuation_r"] == 0.2
    assert short["classification"] == "proper peak capture"

    continuation = by_id["continuation-1"]
    assert continuation["post_exit_continuation_r"] == 2.2
    assert continuation["missed_r"] == 2.2
    assert continuation["classification"] == "premature"


def test_eod_peak_audit_keeps_missing_bars_and_invalid_risk_rows(tmp_path):
    trades_db, bars_db = _create_fixture_databases(tmp_path)
    report = audit_databases(trades_db, bars_db, audit_date="2026-10-17")
    by_id = {row["trade_id"]: row for row in report["trades"]}

    missing = by_id["missing-1"]
    assert missing["status"] == "no_bars"
    assert missing["bar_count"] == 0
    assert missing["realized_r"] == 0.2
    assert missing["in_trade_peak_r"] is None
    assert missing["classification_code"] == "no_bars"

    invalid = by_id["invalid-risk-1"]
    assert invalid["status"] == "invalid_risk"
    assert invalid["risk_per_share"] is None
    assert invalid["realized_r"] is None
    assert invalid["classification_code"] == "invalid_risk"
    assert "recorded_initial_risk_invalid" in invalid["data_issues"]


def test_input_databases_open_read_only(tmp_path):
    trades_db, _bars_db = _create_fixture_databases(tmp_path)
    connection = connect_read_only(trades_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE should_not_exist (id INTEGER)")
    finally:
        connection.close()


def test_cli_smoke_outputs_json_and_csv(tmp_path):
    trades_db, bars_db = _create_fixture_databases(tmp_path)
    report_path = tmp_path / "audit.json"
    csv_path = tmp_path / "audit.csv"
    script = str((__import__("scripts.audit_pis_eod_peak", fromlist=["__file__"]).__file__))

    command = [
        sys.executable,
        script,
        "--trades-db",
        str(trades_db),
        "--bars-db",
        str(bars_db),
        "--date",
        "2026-10-17",
        "--session-close-utc",
        "20:00",
        "--out",
        str(report_path),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    stdout_report = json.loads(completed.stdout)
    assert stdout_report["trade_count"] == 5
    assert json.loads(report_path.read_text(encoding="utf-8"))["trade_count"] == 5

    command[-1] = str(csv_path)
    completed_csv = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed_csv.returncode == 0, completed_csv.stderr
    assert "trade_id" in csv_path.read_text(encoding="utf-8").splitlines()[0]
