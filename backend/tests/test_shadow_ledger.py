"""Shadow ledger tests: capture, persistence, forward-return resolution."""
from __future__ import annotations

import contextvars
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.shadow_ledger import ShadowLedger


@pytest.fixture()
def ledger(tmp_path):
    led = ShadowLedger(db_path=tmp_path / "shadow.db")
    yield led


def _sig(**over):
    base = dict(
        symbol="AAPL", action="enter_long", strategy="momentum",
        gate="pipeline", reason="Trade Score 69.4 < 70.0 (raw=69.4)",
        price=100.0, stop_loss=99.0, take_profit=None, atr14=0.5,
        confidence=0.8,
        sig=SimpleNamespace(ml_probability=0.67, candidate_id="CAND-1"),
    )
    base.update(over)
    return base


def _frame_from(entry_minute: datetime, hours: float = 3.0, drift: float = 0.0):
    ts = [entry_minute - timedelta(minutes=m) for m in range(60, 0, -1)]
    ts += [entry_minute + timedelta(minutes=m) for m in range(0, int(hours * 60))]
    n = len(ts)
    px = [100.0 + drift * i for i in range(n)]
    df = pd.DataFrame(
        {
            "open": px, "high": [p + 0.1 for p in px], "low": [p - 0.1 for p in px],
            "close": px, "volume": [1000.0] * n,
        },
        index=pd.DatetimeIndex([t.replace(tzinfo=UTC) for t in ts]),
    )
    return df


def _flush(ledger: ShadowLedger) -> None:
    # writer thread flushes every 2s; poll until the row lands (max ~4s)
    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        if ledger.query(limit=10)["entries"]:
            return
        time.sleep(0.25)
    pytest.fail("shadow row never persisted")


def test_record_and_query_roundtrip(ledger):
    ledger.record(**_sig())
    _flush(ledger)
    data = ledger.query(limit=10)
    assert data["summary"] == {} or all(isinstance(v, int) for v in data["summary"].values())
    e = data["entries"][0]
    assert e["symbol"] == "AAPL"
    assert e["gate"] == "pipeline"
    assert e["ml_probability"] == 0.67
    assert e["candidate_id"] == "CAND-1"
    assert e["trade_score"] == 69.4  # parsed from reason text
    assert e["resolved"] == 0
    assert data["gate_summary"][0]["gate"] == "pipeline"
    assert data["gate_summary"][0]["total"] == 1


def test_writer_reconnects_after_startup_connect_failure(tmp_path, monkeypatch):
    original_connect = ShadowLedger._connect
    first_attempt = threading.Event()
    release_attempt = threading.Event()
    attempts = 0

    def flaky_connect(self):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt.set()
            release_attempt.wait(timeout=2)
            raise RuntimeError("temporary ledger connection failure")
        return original_connect(self)

    monkeypatch.setattr(ShadowLedger, "_connect", flaky_connect)
    led = ShadowLedger(db_path=tmp_path / "reconnect.db")
    assert first_attempt.wait(timeout=2)
    led.record(**_sig())
    release_attempt.set()

    assert led.flush(5) is True
    assert led.query(limit=10)["entries"][0]["symbol"] == "AAPL"


def test_record_deduplicates_same_candidate_and_gate(ledger):
    ledger.record(**_sig(reason="first reason"))
    ledger.record(**_sig(reason="duplicate reason"))
    assert ledger.flush(5) is True
    rows = ledger.query(limit=10)["entries"]
    assert len(rows) == 1
    assert rows[0]["reason"] == "first reason"


def test_record_allows_same_candidate_across_gates(ledger):
    ledger.record(**_sig(gate="pipeline"))
    ledger.record(**_sig(gate="portfolio_or_risk"))
    assert ledger.flush(5) is True
    assert len(ledger.query(limit=10)["entries"]) == 2


def test_meta_prob_parsed_from_reason(ledger):
    ledger.record(**_sig(reason="Meta-label REJECT [REAL_MODEL]: prob_0.4991_below_threshold_0.52"))
    _flush(ledger)
    assert ledger.query(limit=1)["entries"][0]["meta_prob"] == pytest.approx(0.4991)


def test_record_preserves_explicit_lineage_and_sanitizes_non_finite_values(ledger):
    signal_ts = datetime(2026, 10, 21, 14, 29, tzinfo=UTC)
    ledger.record(
        **_sig(
            price=float("nan"),
            stop_loss=float("inf"),
            confidence=float("-inf"),
            candidate_id="EXPLICIT-CANDIDATE",
            decision_bar_timestamp_utc=datetime(2026, 10, 21, 14, 30, tzinfo=UTC),
            sig=SimpleNamespace(
                ml_probability=0.67,
                candidate_id="SIGNAL-CANDIDATE",
                ts=signal_ts,
            ),
        )
    )
    _flush(ledger)
    row = ledger.query(limit=1)["entries"][0]
    assert row["candidate_id"] == "EXPLICIT-CANDIDATE"
    assert row["signal_timestamp_utc"] == signal_ts.isoformat()
    assert row["decision_bar_timestamp_utc"] == "2026-10-21T14:30:00+00:00"
    assert row["session_date"] == "2026-10-21"
    assert row["signal_price"] is None
    assert row["stop_loss"] is None
    assert row["confidence"] is None


def test_schema_migrates_legacy_ledger_before_new_indexes(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE shadow_entries (
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
            resolved INTEGER NOT NULL DEFAULT 0,
            px_5m REAL, px_15m REAL, px_30m REAL, px_60m REAL,
            fav_pct_60m REAL, mfe_pct_60m REAL, mae_pct_60m REAL,
            r_fav_60m REAL, verdict TEXT
        )
        """
    )
    con.commit()
    con.close()

    ShadowLedger(db_path=path)
    with sqlite3.connect(path) as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(shadow_entries)")}
    assert {"signal_timestamp_utc", "decision_bar_timestamp_utc"} <= columns


def test_missed_winner_payload_excludes_avoided_loser(tmp_path):
    """The recovery/analytics payload is strictly MISSED_WINNER-only."""
    led = ShadowLedger(db_path=tmp_path / "verdict-filter.db")
    led.record(**_sig(symbol="AAPL"))
    led.record(**_sig(symbol="MSFT"))
    led.record(**_sig(symbol="GOOG"))
    _flush(led)

    import sqlite3
    con = sqlite3.connect(led._db_path)
    con.execute(
        "UPDATE shadow_entries SET resolved=1, verdict=?, fav_pct_60m=? WHERE symbol=?",
        ("MISSED_WINNER", 1.0, "AAPL"),
    )
    con.execute(
        "UPDATE shadow_entries SET resolved=1, verdict=?, fav_pct_60m=? WHERE symbol=?",
        ("AVOIDED_LOSER", -1.0, "MSFT"),
    )
    con.execute(
        "UPDATE shadow_entries SET resolved=1, verdict=?, fav_pct_60m=? WHERE symbol=?",
        ("MISSED_WINNER", 0.5, "GOOG"),
    )
    con.commit()
    con.close()

    data = led.query(limit=1)
    assert [row["symbol"] for row in data["missed_winners"]] == ["AAPL", "GOOG"]


def test_resolver_marks_winner_long(tmp_path):
    led = ShadowLedger(db_path=tmp_path / "s.db")
    entry_min = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=90)
    led.record(created_at_iso=None) if False else None
    # insert directly with backdated created_at so it is fully elapsed
    led.record(**_sig())
    _flush(led)
    import sqlite3
    con = sqlite3.connect(led._db_path)
    con.execute("UPDATE shadow_entries SET created_at=?", (entry_min.isoformat(),))
    con.commit()
    con.close()

    rising = _frame_from(entry_min, hours=2.0, drift=0.08)  # +0.08/bar -> +4.8% over 60m

    class FakeData:
        def frame(self, symbol, timeframe, limit=600):
            return rising

    import asyncio
    n = asyncio.run(led.resolve_pending(FakeData()))
    assert n == 1
    row = led.query(limit=1)["entries"][0]
    assert row["resolved"] == 1
    assert row["verdict"] == "MISSED_WINNER"
    assert row["fav_pct_60m"] is not None and row["fav_pct_60m"] > 0
    assert row["mfe_pct_60m"] >= row["fav_pct_60m"]
    assert row["r_fav_60m"] is not None and row["r_fav_60m"] > 1.0


def test_resolver_anchors_to_decision_bar_timestamp(tmp_path):
    """Forward returns use the rejected signal's bar, not the ledger write time."""
    led = ShadowLedger(db_path=tmp_path / "decision-bar.db")
    decision_bar = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=90)
    sig = SimpleNamespace(
        ml_probability=0.67,
        candidate_id="CAND-DECISION-BAR",
        decision_bar_timestamp_utc=decision_bar,
    )
    led.record(**_sig(sig=sig))
    _flush(led)

    rising = _frame_from(decision_bar, hours=1.2, drift=0.08)

    class FakeData:
        def frame(self, symbol, timeframe, limit=600):
            return rising

    import asyncio
    assert asyncio.run(led.resolve_pending(FakeData())) == 1
    row = led.query(limit=1)["entries"][0]
    assert row["decision_bar_timestamp_utc"] == decision_bar.isoformat()
    assert row["verdict"] == "MISSED_WINNER"


def test_resolver_history_fallback_when_rolling_frame_ends_before_horizon(tmp_path):
    led = ShadowLedger(db_path=tmp_path / "history-fallback.db")
    entry_min = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=90)
    led.record(**_sig())
    _flush(led)
    import sqlite3
    con = sqlite3.connect(led._db_path)
    con.execute("UPDATE shadow_entries SET created_at=?", (entry_min.isoformat(),))
    con.commit()
    con.close()

    partial = _frame_from(entry_min, hours=0.25, drift=0.0)
    complete = _frame_from(entry_min, hours=1.5, drift=0.08)

    class FakeData:
        def frame(self, symbol, timeframe, limit=600):
            return partial

        async def history(self, symbol, timeframe, start, end):
            return complete

    import asyncio
    assert asyncio.run(led.resolve_pending(FakeData())) == 1
    row = led.query(limit=1)["entries"][0]
    assert row["verdict"] == "MISSED_WINNER"
    assert row["px_60m"] is not None


def test_resolver_marks_avoided_loser_short(tmp_path):
    led = ShadowLedger(db_path=tmp_path / "s2.db")
    entry_min = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=90)
    led.record(**_sig(action="enter_short"))
    _flush(led)
    import sqlite3
    con = sqlite3.connect(led._db_path)
    con.execute("UPDATE shadow_entries SET created_at=?", (entry_min.isoformat(),))
    con.commit()
    con.close()

    rising = _frame_from(entry_min, hours=2.0, drift=0.08)  # short into a rally -> loser avoided

    class FakeData:
        def frame(self, symbol, timeframe, limit=600):
            return rising

    import asyncio
    asyncio.run(led.resolve_pending(FakeData()))
    row = led.query(limit=1)["entries"][0]
    assert row["verdict"] == "AVOIDED_LOSER"
    assert row["fav_pct_60m"] < 0


def test_resolver_leaves_recent_rows_unresolved(tmp_path):
    led = ShadowLedger(db_path=tmp_path / "s3.db")
    led.record(**_sig())  # created now -> only a few bars elapsed
    _flush(led)

    frame = _frame_from(datetime.now(UTC).replace(second=0, microsecond=0), 0.05)

    class FakeData:
        def frame(self, symbol, timeframe, limit=600):
            return frame

    import asyncio
    asyncio.run(led.resolve_pending(FakeData()))
    row = led.query(limit=1)["entries"][0]
    assert row["resolved"] == 0 and row["verdict"] is None


def test_resolver_does_not_age_data_outage_into_no_bars(tmp_path):
    led = ShadowLedger(db_path=tmp_path / "outage.db")
    led.record(**_sig())
    _flush(led)
    import sqlite3
    con = sqlite3.connect(led._db_path)
    old = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    con.execute("UPDATE shadow_entries SET created_at=?", (old,))
    con.commit()
    con.close()

    class UnavailableData:
        def frame(self, symbol, timeframe, limit=600):
            raise TimeoutError("provider unavailable")

        async def history(self, symbol, timeframe, start, end):
            raise TimeoutError("provider unavailable")

    import asyncio
    assert asyncio.run(led.resolve_pending(UnavailableData())) == 0
    row = led.query(limit=1)["entries"][0]
    assert row["resolved"] == 0
    assert row["verdict"] is None


def test_disabled_ledger_drops_rows_and_skips_resolution(tmp_path):
    """Replay isolation: replays flip `enabled` off so synthetic rejections
    never reach the production ledger (2026-08-26 pollution finding)."""
    led = ShadowLedger(db_path=tmp_path / "s4.db")
    led.enabled = False
    led.record(**_sig())
    assert led._queue.empty() is True

    import asyncio

    assert asyncio.run(led.resolve_pending(None)) == 0


def test_context_disabled_only_suppresses_that_context(tmp_path):
    led = ShadowLedger(db_path=tmp_path / "context.db")
    with led.disabled():
        suppressed = contextvars.copy_context()

    suppressed.run(lambda: led.record(**_sig(symbol="MSFT")))
    led.record(**_sig(symbol="AAPL"))
    assert led.flush(5) is True
    assert [row["symbol"] for row in led.query(limit=10)["entries"]] == ["AAPL"]


def test_resolver_ages_partial_horizon_to_no_bars(tmp_path):
    led = ShadowLedger(db_path=tmp_path / "partial.db")
    entry_min = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=12)
    led.record(**_sig())
    _flush(led)
    import sqlite3
    con = sqlite3.connect(led._db_path)
    con.execute("UPDATE shadow_entries SET created_at=?", (entry_min.isoformat(),))
    con.commit()
    con.close()

    idx = pd.DatetimeIndex(
        [entry_min, entry_min + timedelta(minutes=1)],
        tz=UTC,
    )
    partial = pd.DataFrame(
        {
            "open": [100.0, 200.0],
            "high": [100.0, 200.0],
            "low": [100.0, 200.0],
            "close": [100.0, 200.0],
        },
        index=idx,
    )

    class FakeData:
        def frame(self, symbol, timeframe, limit=600):
            return partial

    import asyncio
    assert asyncio.run(led.resolve_pending(FakeData())) == 1
    row = led.query(limit=1)["entries"][0]
    assert row["verdict"] == "NO_BARS"
    assert row["px_60m"] is None


@pytest.mark.asyncio
async def test_resolver_loop_reads_current_data_manager(monkeypatch):
    import asyncio

    import app.core.shadow_ledger as shadow_module

    current = object()
    seen = []

    class FakeLedger:
        async def resolve_pending(self, data_manager):
            seen.append(data_manager)
            raise asyncio.CancelledError

    monkeypatch.setattr(shadow_module, "shadow_ledger", FakeLedger())
    with pytest.raises(asyncio.CancelledError):
        await shadow_module.resolver_loop(lambda: current, interval_sec=0)
    assert seen == [current]
