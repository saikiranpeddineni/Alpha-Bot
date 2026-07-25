"""Unit and integration test suite for Deterministic Production Replay System."""

import json
from datetime import datetime, timezone
from pathlib import Path
import pytest

from app.config import Settings
from app.core.engine import TradingEngine
from app.core.runtime import TradingRuntime
from app.db.database import Database
from app.replay.broker import ReplayBroker
from app.replay.clock import ReplayClock
from app.replay.ledger import ReplayLedger
from app.replay.parity_validator import ProductionParityValidator
from app.replay.runtime import ReplayRuntime
from app.replay.session import ReplaySessionManager


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test_replay.db"
    return Database(db_path)


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        mode="paper",
        db_path=str(tmp_path / "test_replay.db"),
    )


def test_replay_clock_advancement():
    t0 = datetime(2026, 7, 24, 9, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(start_dt=t0)
    assert clock.now_utc() == t0

    t1 = datetime(2026, 7, 24, 9, 31, 0, tzinfo=timezone.utc)
    clock.set_time(t1)
    assert clock.now_utc() == t1


@pytest.mark.asyncio
async def test_replay_broker_order_lifecycle():
    t0 = datetime(2026, 7, 24, 9, 30, 0, tzinfo=timezone.utc)
    broker = ReplayBroker(starting_cash=100_000.0, slippage_bps=0.0, fill_model="ohlc_touch")
    broker.set_time(t0)

    account = await broker.account()
    assert account.equity == 100_000.0
    assert account.cash == 100_000.0

    # Process OHLC tick
    broker.process_ohlc("NVDA", 200.0, 205.0, 195.0, 202.0, t0)
    acct_after = await broker.account()
    assert acct_after.equity == 100_000.0


def test_replay_session_cancellation(tmp_path: Path):
    sess_mgr = ReplaySessionManager(db_path=tmp_path / "replay_sessions.db")
    session = sess_mgr.create_session("rpl_test_123", "2026-07-24", "2026-07-24", {"mode": "paper"})
    assert session["status"] == "CREATED"

    sess_mgr.request_cancel("rpl_test_123")
    assert sess_mgr.is_cancelled("rpl_test_123")
    updated = sess_mgr.get_session("rpl_test_123")
    assert updated["status"] == "CANCELLED"


def test_replay_parity_validator_hashes():
    ledger_a = ReplayLedger("rpl_a")
    ledger_b = ReplayLedger("rpl_b")

    t0 = datetime(2026, 7, 24, 9, 30, 0, tzinfo=timezone.utc)
    ledger_a.record_event("SCANNER_CANDIDATE", t0, "NVDA", {"score": 85.0})
    ledger_b.record_event("SCANNER_CANDIDATE", t0, "NVDA", {"score": 85.0})

    ledger_a.record_equity(t0, 100000.0, 100000.0, 200000.0, 0)
    ledger_b.record_equity(t0, 100000.0, 100000.0, 200000.0, 0)

    report = ProductionParityValidator.validate_passes(ledger_a, ledger_b)
    assert report["overall_match"] is True
    assert report["determinism_verdict"] == "BIT_FOR_BIT_IDENTICAL_PARITY"


def test_replay_runtime_2_pass_determinism(test_settings: Settings, tmp_db: Database, tmp_path: Path):
    sess_mgr = ReplaySessionManager(db_path=tmp_path / "replay_sessions.db")
    runtime = ReplayRuntime(test_settings, tmp_db, replay_id="rpl_det_test", session_manager=sess_mgr)
    
    res = runtime.run_replay("2026-07-24", "2026-07-24", symbols=["NVDA"], verify_parity_pass=True)
    assert res["session"]["status"] == "COMPLETED"
    assert res["parity"]["overall_match"] is True
    assert res["parity"]["determinism_verdict"] == "BIT_FOR_BIT_IDENTICAL_PARITY"
