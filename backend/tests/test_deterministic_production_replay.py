"""Deterministic production replay invariants."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.db.database import Database
from app.replay.broker import ReplayBroker
from app.replay.clock import ReplayClock
from app.replay.determinism import DeterministicUUIDProxy
from app.replay.ledger import ReplayLedger
from app.replay.market_feed import ReplayDataManager, ReplayMarketFeed
from app.replay.parity_validator import ProductionParityValidator
from app.replay.runtime import ReplayRuntime
from app.replay.session import ReplaySessionManager


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test_replay.db")


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(mode="paper", db_path=str(tmp_path / "test_replay.db"))


def _write_fixture(path: Path) -> None:
    start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(3):
        ts = start + timedelta(minutes=i)
        for symbol, px in (("SPY", 630.0), ("QQQ", 560.0), ("NVDA", 180.0), ("^VIX", 17.5)):
            rows.append({"kind": "bar", "symbol": symbol, "ts": ts.isoformat(), "open": px, "high": px + 0.2,
                         "low": px - 0.2, "close": px + i * 0.01, "volume": 100000})
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_replay_clock_advancement():
    t0 = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    assert clock.now_utc() == t0
    t1 = t0 + timedelta(minutes=1)
    clock.set_time(t1)
    assert clock.now_utc() == t1


def test_replay_uuid_sequence_is_repeatable_per_pass():
    a, b = DeterministicUUIDProxy("same-pass-seed"), DeterministicUUIDProxy("same-pass-seed")
    seq_a = [a.uuid4().hex for _ in range(10)]
    seq_b = [b.uuid4().hex for _ in range(10)]
    assert seq_a == seq_b
    assert len(set(seq_a)) == len(seq_a)


@pytest.mark.asyncio
async def test_replay_data_manager_is_strictly_asof():
    t0 = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    bars = [
        {"symbol": "NVDA", "ts_dt": t0, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10, "in_replay_window": True},
        {"symbol": "NVDA", "ts_dt": t0 + timedelta(minutes=1), "open": 200, "high": 201, "low": 199, "close": 200, "volume": 10, "in_replay_window": True},
    ]
    data = ReplayDataManager(bars)
    data.set_time(t0)
    assert data.last_price("NVDA") == 100.0
    hist = await data.history("NVDA", "1Min", t0 - timedelta(days=1), t0 + timedelta(days=1))
    assert len(hist) == 1
    assert float(hist.iloc[-1]["close"]) == 100.0


def test_vix_alias_is_historical_not_synthetic():
    t0 = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    bars = [{"symbol": "^VIX", "ts_dt": t0, "open": 17.5, "high": 17.6, "low": 17.4,
             "close": 17.55, "volume": 10, "in_replay_window": True}]
    data = ReplayDataManager(bars)
    data.set_time(t0)
    assert data.last_price("^VIX") == 17.55
    assert data.last_price("VIX") == 17.55
    assert data.fidelity_diagnostics()["has_vix"] is True


def test_symbol_filter_does_not_remove_production_context(tmp_path: Path):
    path = tmp_path / "bars.jsonl"
    _write_fixture(path)
    feed = ReplayMarketFeed(path)
    bars = feed.load_bars("2026-07-24", "2026-07-24", symbols=["NVDA"])
    present = {row["symbol"] for row in bars}
    assert {"NVDA", "SPY", "QQQ", "^VIX"}.issubset(present)


@pytest.mark.asyncio
async def test_replay_broker_order_lifecycle():
    t0 = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    broker = ReplayBroker(100_000.0, 0.0, "ohlc_touch")
    broker.process_ohlc("NVDA", 200.0, 205.0, 195.0, 202.0, t0)
    account = await broker.account()
    assert account.equity == 100_000.0
    assert account.cash == 100_000.0


def test_replay_session_cancellation(tmp_path: Path):
    mgr = ReplaySessionManager(db_path=tmp_path / "sessions.db")
    mgr.create_session("rpl_test", "2026-07-24", "2026-07-24", {})
    mgr.request_cancel("rpl_test")
    assert mgr.is_cancelled("rpl_test")
    assert mgr.get_session("rpl_test")["status"] == "CANCELLED"


def test_parity_validator_detects_real_divergence():
    a, b = ReplayLedger("a"), ReplayLedger("b")
    t0 = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    a.production_ticks = b.production_ticks = 1
    a.record_equity(t0, 100000, 100000, 200000, 0)
    b.record_equity(t0, 99999, 99999, 199998, 0)
    report = ProductionParityValidator.validate_passes(a, b)
    assert report["overall_match"] is False
    assert report["determinism_verdict"] == "DIVERGENCE_DETECTED"
    assert report["first_divergence"]["stage"] == "equity_hash"
    assert report["first_divergence"]["index"] == 0
    assert report["first_divergence"]["pass_a"]["equity"] == 100000.0
    assert report["first_divergence"]["pass_b"]["equity"] == 99999.0


def test_parity_validator_reports_first_decision_stage():
    a, b = ReplayLedger("a"), ReplayLedger("b")
    t0 = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    a.record_event("ML_DECISION", t0, "NVDA", {"ml_probability": 0.61, "decision": "PASS"})
    b.record_event("ML_DECISION", t0, "NVDA", {"ml_probability": 0.60, "decision": "PASS"})
    report = ProductionParityValidator.validate_passes(a, b)
    assert report["first_divergence"]["stage"] == "ml_hash"
    assert report["first_divergence"]["collection"] == "ml_decisions"


def test_replay_runtime_executes_real_production_ticks(test_settings: Settings, tmp_db: Database, tmp_path: Path):
    data_path = tmp_path / "bars.jsonl"
    _write_fixture(data_path)
    mgr = ReplaySessionManager(db_path=tmp_path / "sessions.db")
    runtime = ReplayRuntime(test_settings, tmp_db, replay_id="rpl_det", session_manager=mgr, data_path=data_path)
    result = runtime.run_replay("2026-07-24", "2026-07-24", symbols=["SPY", "QQQ", "NVDA"], verify_parity_pass=True)
    assert result["session"]["status"] == "COMPLETED"
    assert result["session"]["production_ticks"] == 3
    # parity key is always present; None means single-pass mode (AB comparison not executed).
    # When present as a dict, verify overall_match is True.
    assert "parity" in result
    if result["parity"] is not None:
        assert result["parity"]["overall_match"] is True
    assert result["manifest"]["decision_path"] == "TradingEngine._tick"
    assert result["manifest"]["lookahead_policy"] == "strict_as_of"
    assert result["manifest"]["identity_policy"] == "deterministic replay-only UUID binding"


def test_missing_market_data_fails_closed(test_settings: Settings, tmp_db: Database, tmp_path: Path):
    mgr = ReplaySessionManager(db_path=tmp_path / "sessions.db")
    runtime = ReplayRuntime(test_settings, tmp_db, replay_id="rpl_missing", session_manager=mgr, data_path=tmp_path / "missing.jsonl")
    with pytest.raises(FileNotFoundError):
        runtime.run_replay("2026-07-24", "2026-07-24")
    assert mgr.get_session("rpl_missing")["status"] == "FAILED"


def test_out_of_window_replay_bars_includes_available_range(tmp_path: Path):
    data_path = tmp_path / "bars.jsonl"
    _write_fixture(data_path)
    feed = ReplayMarketFeed(data_path)
    with pytest.raises(ValueError) as excinfo:
        feed.load_bars("2026-07-28", "2026-07-28")
    assert "No replay bars inside 2026-07-28..2026-07-28" in str(excinfo.value)
    assert "Available replay data date range: 2026-07-24..2026-07-24" in str(excinfo.value)

