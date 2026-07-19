from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import pandas as pd

from app.core.domain import Regime, Signal, SignalAction, utcnow
from app.db.database import Database
from app.core.engine import TradingEngine, EngineState
from app.strategies import SignalPipeline
from tests.conftest import make_bars


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test_opportunity.db"
    return Database(db_path)


@pytest.mark.asyncio
async def test_resurrection_queue_lifecycle(temp_db: Database):
    # Instantiate TradingEngine in a mocked state
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL", "SPY"]
    settings.risk_config().model_dump.return_value = {}

    engine = TradingEngine(settings, temp_db)
    engine.state = EngineState.RUNNING

    # Mock DataManager
    data_mock = MagicMock()
    df = make_bars(n=100)
    data_mock.frame.return_value = df
    engine.data = data_mock

    # Mock Portfolio
    portfolio_mock = MagicMock()
    portfolio_mock.positions = []
    engine.portfolio = portfolio_mock

    # Mock Pipeline
    pipeline_mock = MagicMock()
    engine.pipeline = pipeline_mock
    engine.broker = MagicMock()
    engine.broker.is_paper = True  # enable resurrection queue (paper/demo mode only)
    engine.risk = MagicMock()

    now = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)

    # 1. Queue insertion test
    sig = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, strategy="orb", price=100.0, confidence=0.6, reason="")
    sig.candidate_id = "TEST_CAND_RES"

    # Mock strategy
    strat_mock = MagicMock()
    strat_mock.name = "orb"
    strat_mock.enabled = True
    strat_mock.params = {}
    strat_mock.on_bar.return_value = sig
    engine.strategies = [strat_mock]

    # Mock pipeline returning rejection
    pipeline_mock.process.return_value = ([], [(sig, "ML confidence low")])

    # Evaluate symbol to trigger rejected queue insertion
    entries = await engine._evaluate_symbol("AAPL", None, 10000.0, now)
    assert len(entries) == 0
    assert len(engine.resurrection_queue) == 1
    assert engine.resurrection_queue[0]["symbol"] == "AAPL"
    assert engine.resurrection_queue[0]["retry_count"] == 0
    assert "original_signal" not in engine.resurrection_queue[0]

    # 2. TTL Expiry Test
    # Manually modify ts_rejected to be 31 minutes ago
    engine.resurrection_queue[0]["ts_rejected"] = now - timedelta(minutes=31)
    
    # Process queue -> should discard expired item
    resurrected = await engine._process_resurrection_queue(10000.0, now)
    assert len(resurrected) == 0
    assert len(engine.resurrection_queue) == 0

    # 3. Successful Resurrection Test
    # Re-insert the candidate
    sig_new = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, strategy="orb", price=100.0, confidence=0.6, reason="")
    sig_new.candidate_id = "TEST_CAND_RES_SUCCESS"
    
    strat_mock.on_bar.return_value = sig_new

    # Write initial candidate record to database
    from app.core.shadow_ledger import ShadowLedger
    ShadowLedger.record_candidate(
        db_engine=temp_db,
        symbol="AAPL",
        ts=now,
        strategy="orb",
        direction="long",
        signal_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        ml_confidence=0.6,
        final_decision="rejected",
        rejection_stage="ml_gate",
        rejection_reason="ML confidence low",
        df=df,
        spy_df=df,
        slot_rvol_val=1.0,
        vwap_telemetry={},
        mode="backtest",
        candidate_id="TEST_CAND_RES_SUCCESS",
    )

    engine.resurrection_queue.append({
        "symbol": "AAPL",
        "strategy": "orb",
        "direction": "long",
        "ts_rejected": now,
        "candidate_id": "TEST_CAND_RES_SUCCESS",
        "retry_count": 0,
    })

    # Mock pipeline to ACCEPT it this time
    pipeline_mock.process.return_value = ([sig_new], [])

    # Process queue -> should resurrect it
    resurrected = await engine._process_resurrection_queue(10000.0, now)
    assert len(resurrected) == 1
    assert resurrected[0][0].symbol == "AAPL"
    assert resurrected[0][0].strategy == "orb"
    assert len(engine.resurrection_queue) == 0

    # Assert that final decision was updated in DB
    db_record = temp_db.get_shadow_signal("TEST_CAND_RES_SUCCESS")
    assert db_record is not None
    assert db_record["final_decision"] == "pipeline_approved"
    assert db_record["rejection_stage"] == "none"


@pytest.mark.asyncio
async def test_heartbeat_sector_leadership(temp_db: Database):
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL", "SPY"]
    settings.risk_config().model_dump.return_value = {}

    engine = TradingEngine(settings, temp_db)

    # Mock DataManager to return custom bar ETF returns
    data_mock = MagicMock()
    # Return 2 bars for any ETF: first bar low close, second bar high close
    bars_win = pd.DataFrame({"close": [100.0, 105.0], "volume": [100, 100], "high": [106.0, 106.0], "low": [99.0, 99.0]}, index=pd.date_range("2026-01-06 14:30", periods=2, freq="1min", tz="UTC"))
    data_mock.frame.return_value = bars_win
    engine.data = data_mock

    now = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)

    # Executing sector check
    engine._run_heartbeat_tasks(now)
    assert engine._last_heartbeat_hour == 15

    # Executing again with same hour index should return early
    engine._run_heartbeat_tasks(now + timedelta(minutes=5))
    assert engine._last_heartbeat_hour == 15


@pytest.mark.asyncio
async def test_resurrection_queue_timezone_safety(temp_db: Database):
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL", "SPY"]
    settings.risk_config().model_dump.return_value = {}

    engine = TradingEngine(settings, temp_db)
    engine.state = EngineState.RUNNING
    engine.data = MagicMock()
    engine.data.frame.return_value = make_bars(n=10)

    # Insert one candidate with timezone-naive ts_rejected
    naive_ts = datetime(2026, 1, 6, 15, 0)
    sig = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, strategy="orb", price=100.0, confidence=0.6, reason="")
    sig.candidate_id = "TEST_NAIVE_TZ"
    
    strat_mock = MagicMock()
    strat_mock.name = "orb"
    strat_mock.enabled = True
    strat_mock.params = {}
    strat_mock.on_bar.return_value = sig
    engine.strategies = [strat_mock]

    engine.resurrection_queue.append({
        "symbol": "AAPL",
        "strategy": "orb",
        "direction": "long",
        "ts_rejected": naive_ts,
        "candidate_id": "TEST_NAIVE_TZ",
        "retry_count": 0,
    })

    # Call _process_resurrection_queue with timezone-aware 'now'
    aware_now = datetime(2026, 1, 6, 15, 10, tzinfo=UTC)
    resurrected = await engine._process_resurrection_queue(10000.0, aware_now)
    # Should not raise TypeError, but instead process safely (expired or evaluated)
    assert isinstance(resurrected, list)


@pytest.mark.asyncio
async def test_resurrection_queue_live_mode_gating(temp_db: Database):
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL"]
    settings.risk_config().model_dump.return_value = {}
    settings.risk_config().ml_confidence_threshold = 0.55

    engine = TradingEngine(settings, temp_db)
    engine.state = EngineState.RUNNING
    engine.data = MagicMock()
    engine.data.frame.return_value = make_bars(n=100)
    engine.portfolio = MagicMock()
    engine.portfolio.positions = []
    engine.risk = MagicMock()
    engine.evidence_evaluator = MagicMock()
    
    # 1. Live mode -> is_paper is False
    engine.broker = MagicMock()
    engine.broker.is_paper = False

    sig = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, strategy="orb", price=100.0, confidence=0.6, reason="")
    
    # Mock strategy
    strat_mock = MagicMock()
    strat_mock.name = "orb"
    strat_mock.enabled = True
    strat_mock.params = {}
    strat_mock.on_bar.return_value = sig
    engine.strategies = [strat_mock]

    # Mock pipeline returning rejection
    engine.pipeline = MagicMock()
    engine.pipeline.process.return_value = ([], [(sig, "ML confidence low")])

    # Evaluate symbol -> should NOT enqueue in live mode!
    now = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
    await engine._evaluate_symbol("AAPL", None, 10000.0, now)
    assert len(engine.resurrection_queue) == 0


@pytest.mark.asyncio
async def test_resurrection_queue_clears_in_live_mode(temp_db: Database):
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL"]
    settings.risk_config().model_dump.return_value = {}
    settings.risk_config().ml_confidence_threshold = 0.55

    engine = TradingEngine(settings, temp_db)
    engine.state = EngineState.RUNNING
    engine.risk = MagicMock()
    engine.risk.should_flatten.return_value = False
    engine.risk.check_halts.return_value = None
    engine.risk.daily_halted = False
    engine.risk.hard_halted = False
    engine.portfolio = MagicMock()
    engine.pipeline = MagicMock()
    engine.evidence_evaluator = MagicMock()
    
    # Pre-populate queue
    engine.resurrection_queue.append({
        "symbol": "AAPL",
        "strategy": "orb",
        "direction": "long",
        "ts_rejected": datetime(2026, 1, 6, 15, 0, tzinfo=UTC),
        "candidate_id": "TEST_STALE_LIVE",
        "retry_count": 0,
    })

    # Live mode -> is_paper is False
    engine.broker = MagicMock()
    engine.broker.is_paper = False
    del engine.broker.poll_fills
    
    # Trigger tick roll / processing
    engine.data = MagicMock()
    async def mock_poll(*args, **kwargs):
        return {"AAPL": make_bars(n=5)}
    engine.data.poll = mock_poll
    async def mock_account():
        return MagicMock(equity=10000.0, cash=10000.0)
    engine.broker.account = mock_account

    async def mock_positions():
        return []
    engine.broker.positions = mock_positions

    async def mock_open_orders():
        return []
    engine.broker.open_orders = mock_open_orders
    engine.clock = MagicMock()
    engine.clock.session_date.return_value = datetime(2026, 1, 6).date()
    
    await engine._tick()
    
    # Should have cleared the queue!
    assert len(engine.resurrection_queue) == 0


@pytest.mark.asyncio
async def test_resurrection_queue_bound(temp_db: Database):
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL"]
    settings.risk_config().model_dump.return_value = {}
    settings.risk_config().ml_confidence_threshold = 0.55

    engine = TradingEngine(settings, temp_db)
    engine.state = EngineState.RUNNING
    engine.risk = MagicMock()
    engine.evidence_evaluator = MagicMock()
    engine.broker = MagicMock()
    engine.broker.is_paper = True

    # Pre-populate 50 items
    for i in range(50):
        engine.resurrection_queue.append({
            "symbol": f"AAPL_{i}",
            "strategy": "orb",
            "direction": "long",
            "ts_rejected": datetime(2026, 1, 6, 15, 0, tzinfo=UTC),
            "candidate_id": f"TEST_CAND_{i}",
            "retry_count": 0,
        })

    sig = Signal(symbol="AAPL_NEW", action=SignalAction.ENTER_LONG, strategy="orb", price=100.0, confidence=0.6, reason="")
    
    # Mock strategy
    strat_mock = MagicMock()
    strat_mock.name = "orb"
    strat_mock.enabled = True
    strat_mock.params = {}
    strat_mock.on_bar.return_value = sig
    engine.strategies = [strat_mock]

    engine.pipeline = MagicMock()
    engine.pipeline.process.return_value = ([], [(sig, "rejected")])
    engine.data = MagicMock()
    engine.data.frame.return_value = make_bars(n=100)
    engine.portfolio = MagicMock()
    engine.portfolio.positions = []

    now = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
    await engine._evaluate_symbol("AAPL_NEW", None, 10000.0, now)

    # Queue size must still be 50, and the oldest item AAPL_0 should be discarded!
    assert len(engine.resurrection_queue) == 50
    assert engine.resurrection_queue[0]["symbol"] == "AAPL_1"
    assert engine.resurrection_queue[-1]["symbol"] == "AAPL_NEW"

