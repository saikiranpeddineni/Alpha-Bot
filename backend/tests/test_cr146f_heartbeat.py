import pytest
import asyncio
import pandas as pd
from datetime import datetime, UTC, timedelta
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock

from app.config import Settings
from app.main import create_app
from app.core.domain import EngineState
from app.core.engine import TradingEngine


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_heartbeat.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    app = create_app(settings)
    
    with TestClient(app) as c:
        # Start engine within the client's managed loop
        c.post("/api/engine/start", json={"mode": "demo", "confirm": "start"})
        yield c


def test_heartbeat_normal_state(client):
    engine = client.app.state.engine
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = pd.DataFrame(index=[datetime.now(UTC)], data={"close": [15.0]})
    engine.data.frame = MagicMock(return_value=vix_df)
    
    # Mock system models and scanner status to be healthy (Normal state)
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    # Assert default state is NORMAL
    assert engine.system_health == "NORMAL"
    assert engine.allow_new_entries is True
    assert engine.vix_unavailable is False
    assert engine.heartbeat_diagnostics["broker_connected"] is True
    assert engine.heartbeat_diagnostics["db_connected"] is True


def test_heartbeat_broker_disconnect(client):
    engine = client.app.state.engine
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = pd.DataFrame(index=[datetime.now(UTC)], data={"close": [15.0]})
    engine.data.frame = MagicMock(return_value=vix_df)
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Mock broker.account to raise connection error
    engine.broker.account = AsyncMock(side_effect=ConnectionError("Alpaca connection failed"))
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.system_health == "DEGRADED"
    assert engine.allow_new_entries is False
    assert engine.heartbeat_diagnostics["broker_connected"] is False


def test_heartbeat_stale_market_data(client):
    engine = client.app.state.engine
    engine.watchlist = ["SPY"]
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Set the test date to be explicitly during market session hours (e.g. 11:00 AM ET / 16:00 UTC)
    test_market_time = datetime(2026, 1, 6, 16, 0, 0, tzinfo=UTC)
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = pd.DataFrame(index=[test_market_time], data={"close": [15.0]})
    
    # Mock stale data frame (timestamp is 20 minutes in past: 10:40 AM ET) for SPY, fresh for VIX
    stale_time = test_market_time - timedelta(minutes=20)
    mock_df_spy = pd.DataFrame(index=[stale_time], data={"close": [100.0]})
    
    def side_effect(symbol, *args, **kwargs):
        if symbol in ("VIX", "^VIX"):
            return vix_df
        return mock_df_spy
        
    engine.data.frame = MagicMock(side_effect=side_effect)
    
    # Set engine state to RUNNING to trigger stale check
    engine.state = EngineState.RUNNING
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(test_market_time))
    finally:
        loop.close()
    
    assert engine.allow_new_entries is False
    assert engine.heartbeat_diagnostics["market_data_fresh"] is False


def test_heartbeat_db_connection_fail(client):
    engine = client.app.state.engine
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = pd.DataFrame(index=[datetime.now(UTC)], data={"close": [15.0]})
    engine.data.frame = MagicMock(return_value=vix_df)
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Mock db.get_setting to fail
    engine.db.get_setting = MagicMock(side_effect=Exception("SQLite connection lost"))
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.system_health == "DEGRADED"
    assert engine.heartbeat_diagnostics["db_connected"] is False


def test_heartbeat_critical_db_write_fail(client):
    engine = client.app.state.engine
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = pd.DataFrame(index=[datetime.now(UTC)], data={"close": [15.0]})
    engine.data.frame = MagicMock(return_value=vix_df)
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Mock db.set_setting to fail (e.g. Disk full or Read-only DB)
    engine.db.set_setting = MagicMock(side_effect=Exception("Database is read-only"))
    
    # Mock emergency flatten
    engine._emergency_flatten_loop = AsyncMock()
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    # Verify critical write failure escalates to HALTED and triggers flatten
    assert engine.system_health == "HALTED"
    assert engine.state == EngineState.HALTED
    assert engine.heartbeat_diagnostics["db_write_ok"] is False


def test_heartbeat_buying_power_check(client):
    engine = client.app.state.engine
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = pd.DataFrame(index=[datetime.now(UTC)], data={"close": [15.0]})
    engine.data.frame = MagicMock(return_value=vix_df)
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Mock low buying power
    class MockAccount:
        buying_power = 500.0
    engine.broker.account = AsyncMock(return_value=MockAccount())
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.allow_new_entries is False
    assert engine.heartbeat_diagnostics["buying_power_ok"] is False


def test_heartbeat_position_reconciliation_failure(client):
    engine = client.app.state.engine
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = pd.DataFrame(index=[datetime.now(UTC)], data={"close": [15.0]})
    engine.data.frame = MagicMock(return_value=vix_df)
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Set mock db lot
    from app.portfolio.tracker import _OpenLot
    engine.portfolio._lots["SPY"] = _OpenLot(qty=10, avg_price=100.0, entry_ts=datetime.now(UTC), strategy="momentum")
    
    # Mock broker positions containing QQQ instead of SPY (mismatch!)
    class MockPosition:
        symbol = "QQQ"
        qty = 10
    engine.broker.positions = AsyncMock(return_value=[MockPosition()])
    
    # Mock valid broker account
    class MockAccount:
        buying_power = 200000.0
    engine.broker.account = AsyncMock(return_value=MockAccount())
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.system_health == "SAFE_MODE"
    assert engine.heartbeat_diagnostics["positions_reconciled"] is False


def test_heartbeat_order_reconciliation_failure(client):
    engine = client.app.state.engine
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = pd.DataFrame(index=[datetime.now(UTC)], data={"close": [15.0]})
    engine.data.frame = MagicMock(return_value=vix_df)
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Mock reconcile submissions to do nothing, preventing background thread pops
    engine._reconcile_pending_submissions = AsyncMock()
    
    # Add pending reconciliation order client ID
    engine.pending_reconciliation_orders = {"cid_123": {"symbol": "SPY", "side": "buy", "qty": 10, "order_type": "market", "limit_price": None}}
    
    # Mock broker to return None when getting client order ID (discrepancy!)
    engine.broker.get_order_by_client_id = AsyncMock(return_value=None)
    
    # Mock valid broker account
    class MockAccount:
        buying_power = 200000.0
    engine.broker.account = AsyncMock(return_value=MockAccount())
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.system_health == "SAFE_MODE"
    assert engine.heartbeat_diagnostics["orders_reconciled"] is False


def test_heartbeat_vix_feed_unavailable(client):
    engine = client.app.state.engine
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Mock data.frame for VIX to fail/return empty
    engine.data.frame = MagicMock(return_value=pd.DataFrame())
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.vix_unavailable is True
    assert engine.system_health == "DEGRADED"
    assert engine.heartbeat_diagnostics["vix_fresh"] is False


def test_heartbeat_weekly_checks(client):
    engine = client.app.state.engine
    
    # Force week number to trigger weekly check
    now = datetime(2026, 1, 6, 12, 0, 0)
    engine._last_heartbeat_week = 1
    
    engine._run_heartbeat_tasks(now + timedelta(days=7))
    # Verify weekly checks ran and updated the week tracker
    assert engine._last_heartbeat_week == 3
