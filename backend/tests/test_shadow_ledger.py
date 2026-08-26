"""Shadow ledger tests: capture, persistence, forward-return resolution."""
from __future__ import annotations

import sys
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


def test_meta_prob_parsed_from_reason(ledger):
    ledger.record(**_sig(reason="Meta-label REJECT [REAL_MODEL]: prob_0.4991_below_threshold_0.52"))
    _flush(ledger)
    assert ledger.query(limit=1)["entries"][0]["meta_prob"] == pytest.approx(0.4991)


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

    frame = _frame_from(datetime.now(UTC).replace(second=0, microsecond=0), minutes := 0.05)

    class FakeData:
        def frame(self, symbol, timeframe, limit=600):
            return frame

    import asyncio
    asyncio.run(led.resolve_pending(FakeData()))
    row = led.query(limit=1)["entries"][0]
    assert row["resolved"] == 0 and row["verdict"] is None


def test_disabled_ledger_drops_rows_and_skips_resolution(tmp_path):
    """Replay isolation: replays flip `enabled` off so synthetic rejections
    never reach the production ledger (2026-08-26 pollution finding)."""
    led = ShadowLedger(db_path=tmp_path / "s4.db")
    led.enabled = False
    led.record(**_sig())
    assert led._queue.empty() is True

    import asyncio

    assert asyncio.run(led.resolve_pending(None)) == 0
