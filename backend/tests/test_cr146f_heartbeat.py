import pytest
import asyncio
import logging
import pandas as pd
from datetime import datetime, UTC, timedelta
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock

from app.config import Settings
from app.main import create_app
from app.core.domain import EngineState, Position, Signal, SignalAction
from app.core.engine import TradingEngine


def make_fresh_df(price=15.0, ts=None, volume=100000.0):
    if ts is None:
        ts = datetime.now(UTC)
    return pd.DataFrame(
        index=[ts],
        data={"open": [price], "high": [price], "low": [price], "close": [price], "volume": [volume]}
    )


def setup_healthy_mocks(engine):
    # Reset override and reconciliation flags
    engine._force_gated = False
    engine.reconciliation_failed = False
    engine.reconciliation_mismatch = False
    engine.system_health = "NORMAL"
    engine.allow_new_entries = True
    
    # Reset pending structures
    engine.pending_reconciliation_orders = {}
    engine.portfolio._lots.clear()
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    engine.data.frame = MagicMock(return_value=make_fresh_df(15.0))
    
    # Mock system models and scanner status to be healthy
    engine._last_scan_date = datetime.now(UTC).date()
    engine.model = MagicMock()
    engine.model.info = MagicMock(return_value={"loaded": True})
    
    # Mock healthy broker account (using thread-safe async def)
    class MockAccount:
        buying_power = 200000.0
        equity = 100000.0
        cash = 100000.0
        
    async def mock_account():
        return MockAccount()
    engine.broker.account = mock_account
    
    # Mock empty positions
    async def mock_positions():
        return []
    engine.broker.positions = mock_positions


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_heartbeat.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    
    # Stub out the background loop so it doesn't spawn asynchronous background ticks during tests
    original_run_loop = TradingEngine._run_loop
    TradingEngine._run_loop = AsyncMock()
    
    app = create_app(settings)
    
    with TestClient(app) as c:
        # Start engine within the client's managed loop
        c.post("/api/engine/start", json={"mode": "demo", "confirm": "start"})
        yield c
        
    TradingEngine._run_loop = original_run_loop


def test_heartbeat_normal_state(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
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
    setup_healthy_mocks(engine)
    
    # Mock broker.account to raise connection error
    async def mock_account_fail():
        raise ConnectionError("Alpaca connection failed")
    engine.broker.account = mock_account_fail
    
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
    setup_healthy_mocks(engine)
    engine.watchlist = ["SPY"]
    
    # Set the test date to be explicitly during market session hours (e.g. 11:00 AM ET / 16:00 UTC)
    test_market_time = datetime(2026, 1, 6, 16, 0, 0, tzinfo=UTC)
    
    # Mock VIX frame to be fresh so it is not marked as unavailable
    vix_df = make_fresh_df(15.0, test_market_time)
    
    # Mock stale data frame (timestamp is 20 minutes in past: 10:40 AM ET) for SPY, fresh for VIX
    stale_time = test_market_time - timedelta(minutes=20)
    mock_df_spy = make_fresh_df(100.0, stale_time)
    
    def side_effect(symbol, *args, **kwargs):
        if symbol in ("VIX", "^VIX"):
            return vix_df
        return mock_df_spy
        
    engine.data.frame = MagicMock(side_effect=side_effect)
    
    # Set engine state to RUNNING in paper mode to trigger stale check
    engine.state = EngineState.RUNNING
    engine.mode = "paper"
    engine.data.provider = AsyncMock()
    engine.data.provider.name = "alpaca"
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(test_market_time))
    finally:
        loop.close()
    
    assert engine.allow_new_entries is False
    assert engine.heartbeat_diagnostics["market_data_fresh"] is False


def test_heartbeat_db_connection_fail(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
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
    setup_healthy_mocks(engine)
    
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
    setup_healthy_mocks(engine)
    
    # Mock low buying power
    class MockAccount:
        buying_power = 500.0
        equity = 100000.0
        cash = 100000.0
        
    async def mock_account_low():
        return MockAccount()
    engine.broker.account = mock_account_low
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.allow_new_entries is False
    assert engine.heartbeat_diagnostics["buying_power_ok"] is False


def test_heartbeat_position_reconciliation_failure(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Set mock db lot
    from app.portfolio.tracker import _OpenLot
    engine.portfolio._lots["SPY"] = _OpenLot(qty=10, avg_price=100.0, entry_ts=datetime.now(UTC), strategy="momentum")
    
    # Mock broker positions containing QQQ instead of SPY (mismatch!)
    async def mock_positions_mismatch():
        return [Position(symbol="QQQ", qty=10.0, avg_entry_price=100.0, market_price=100.0)]
    engine.broker.positions = mock_positions_mismatch
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.system_health == "SAFE_MODE"
    assert engine.heartbeat_diagnostics["positions_reconciled"] is False


def test_heartbeat_order_reconciliation_failure(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Add pending reconciliation order client ID
    engine.pending_reconciliation_orders = {"cid_123": {"symbol": "SPY", "side": "buy", "qty": 10, "order_type": "market", "limit_price": None}}
    
    # Mock broker to return None when getting client order ID (discrepancy!)
    async def mock_get_order(client_id):
        return None
    engine.broker.get_order_by_client_id = mock_get_order
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.system_health == "SAFE_MODE"
    assert engine.heartbeat_diagnostics["orders_reconciled"] is False


def test_heartbeat_vix_feed_unavailable_allows_defensive_entries(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Mock data.frame for VIX to fail/return empty
    engine.data.frame = MagicMock(return_value=pd.DataFrame())
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.vix_unavailable is True
    assert engine.system_health == "NORMAL"
    assert engine.heartbeat_diagnostics["vix_fresh"] is False
    assert engine.last_safety_decision.market_risk_state == "DEFENSIVE"
    assert engine.last_safety_decision.allow_new_entries is True
    assert engine.last_safety_decision.risk_multiplier == 0.50


def test_heartbeat_vix_unavailable_plus_stale_market_data_blocks_entries(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    engine.watchlist = ["SPY"]
    engine.mode = "paper"
    engine.data.provider = AsyncMock()
    engine.data.provider.name = "alpaca"
    engine.state = EngineState.RUNNING
    
    test_market_time = datetime(2026, 1, 6, 16, 0, 0, tzinfo=UTC)
    stale_time = test_market_time - timedelta(minutes=20)
    mock_df_spy = make_fresh_df(100.0, stale_time)
    
    def side_effect(symbol, *args, **kwargs):
        if symbol in ("VIX", "^VIX"):
            return pd.DataFrame()
        return mock_df_spy
        
    engine.data.frame = MagicMock(side_effect=side_effect)
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(test_market_time))
    finally:
        loop.close()
        
    assert engine.vix_unavailable is True
    assert engine.system_health == "NORMAL"  # market data check fails but allow_new_entries is False
    assert engine.allow_new_entries is False
    assert engine.last_safety_decision.allow_new_entries is False


def test_heartbeat_vix_unavailable_plus_reconciliation_failure_blocks_entries(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    engine.reconciliation_mismatch = True
    engine.data.frame = MagicMock(return_value=pd.DataFrame())
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
        
    assert engine.vix_unavailable is True
    assert engine.system_health == "SAFE_MODE"
    assert engine.allow_new_entries is False
    assert engine.last_safety_decision.allow_new_entries is False


def test_heartbeat_vix_state_transition_logging(client, caplog):
    caplog.set_level(logging.INFO)
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    now = datetime.now(UTC)
    loop = asyncio.new_event_loop()
    try:
        # 1. VIX unavailable -> Warn ONCE
        engine.data.frame = MagicMock(return_value=pd.DataFrame())
        caplog.clear()
        loop.run_until_complete(engine._check_system_heartbeat(now))
        warn_count_1 = sum(1 for rec in caplog.records if "VIX volatility feed unavailable" in rec.message)
        assert warn_count_1 == 1
        assert engine._vix_unavailable_last_state is True
        
        # 2. Remains unavailable -> No repeated warning
        caplog.clear()
        loop.run_until_complete(engine._check_system_heartbeat(now))
        warn_count_2 = sum(1 for rec in caplog.records if "VIX volatility feed unavailable" in rec.message)
        assert warn_count_2 == 0
        assert engine._vix_unavailable_last_state is True
        
        # 3. VIX recovers -> Info recovery ONCE
        fresh_df = make_fresh_df(15.0, now)
        engine.data.frame = MagicMock(return_value=fresh_df)
        caplog.clear()
        loop.run_until_complete(engine._check_system_heartbeat(now))
        info_recovery_count = sum(1 for rec in caplog.records if "VIX volatility feed restored" in rec.message)
        assert info_recovery_count == 1
        assert engine._vix_unavailable_last_state is False
        
        # 4. Remains healthy -> No repeated recovery message
        caplog.clear()
        loop.run_until_complete(engine._check_system_heartbeat(now))
        info_recovery_count_2 = sum(1 for rec in caplog.records if "VIX volatility feed restored" in rec.message)
        assert info_recovery_count_2 == 0
        assert engine._vix_unavailable_last_state is False
        
        # 5. VIX fails again -> New warning ONCE
        engine.data.frame = MagicMock(return_value=pd.DataFrame())
        caplog.clear()
        loop.run_until_complete(engine._check_system_heartbeat(now))
        warn_count_3 = sum(1 for rec in caplog.records if "VIX volatility feed unavailable" in rec.message)
        assert warn_count_3 == 1
        assert engine._vix_unavailable_last_state is True
    finally:
        loop.close()


def test_heartbeat_weekly_checks(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Force week number to trigger weekly check
    now = datetime(2026, 1, 6, 12, 0, 0)
    engine._last_heartbeat_week = 1
    
    engine._run_heartbeat_tasks(now + timedelta(days=7))
    # Verify weekly checks ran and updated the week tracker
    assert engine._last_heartbeat_week == 3


def test_dynamic_gating_manual_override(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)

    # Manual gate block via API
    r = client.post("/api/system/allow-new-entries", json={"allow": False})
    assert r.status_code == 200
    assert r.json()["allow_new_entries"] is False
    assert r.json()["system_health"] == "DEGRADED"
    assert engine._force_gated is True

    # Execute check_system_heartbeat - verify override keeps entries blocked
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.allow_new_entries is False
    assert engine.system_health == "DEGRADED"

    # Manual gate enable via API
    r = client.post("/api/system/allow-new-entries", json={"allow": True})
    assert r.status_code == 200
    assert r.json()["allow_new_entries"] is True
    assert r.json()["system_health"] == "NORMAL"
    assert engine._force_gated is False


def test_dynamic_recovery_and_latching(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)

    # 1. Mock transient failure: broker disconnect
    async def mock_account_fail():
        raise ConnectionError("Alpaca failed")
    engine.broker.account = mock_account_fail

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
    
    assert engine.system_health == "DEGRADED"
    assert engine.allow_new_entries is False

    # 2. Broker connection recovers -> Verify auto-recovery to NORMAL
    class MockAccount:
        buying_power = 200000.0
        equity = 100000.0
        cash = 100000.0
        
    async def mock_account_ok():
        return MockAccount()
    engine.broker.account = mock_account_ok
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()
        
    assert engine.system_health == "NORMAL"
    assert engine.allow_new_entries is True

    # 3. Position mismatch occurs -> Verify latching to SAFE_MODE
    from app.portfolio.tracker import _OpenLot
    engine.portfolio._lots["SPY"] = _OpenLot(qty=10, avg_price=100.0, entry_ts=datetime.now(UTC), strategy="momentum")
    
    async def mock_positions_mismatch():
        return [Position(symbol="QQQ", qty=10.0, avg_entry_price=100.0, market_price=100.0)]
    engine.broker.positions = mock_positions_mismatch

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()

    assert engine.system_health == "SAFE_MODE"
    assert engine.allow_new_entries is False
    assert engine.reconciliation_mismatch is True

    # 4. Position mismatch is fixed in next heartbeat -> Verify SAFE_MODE remains latched!
    # Mock broker positions to now perfectly match DB lot
    async def mock_positions_match():
        return [Position(symbol="SPY", qty=10.0, avg_entry_price=100.0, market_price=100.0)]
    engine.broker.positions = mock_positions_match

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()

    # Still latched!
    assert engine.system_health == "SAFE_MODE"
    assert engine.allow_new_entries is False
    assert engine.reconciliation_mismatch is True

    # 5. Call manual reset-health -> Verify latch clears and recovers to NORMAL
    r = client.post("/api/system/reset-health")
    assert r.status_code == 200
    assert r.json()["system_health"] == "NORMAL"
    assert r.json()["allow_new_entries"] is True
    assert engine.reconciliation_mismatch is False


def test_vix_unavailable_defensive_risk_multiplier_sizing_propagation(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    from app.risk.manager import AccountContext
    ctx = AccountContext(
        equity=100000.0, cash=100000.0, positions=[], day_pnl=0.0,
        session_start_equity=100000.0, peak_equity=100000.0, day_trades_today=0, is_paper=True
    )
    df = make_fresh_df(100.0)
    
    # 1. Normal state signal evaluation (risk_multiplier = 1.0)
    sig_normal = Signal(
        symbol="SPY", action=SignalAction.ENTER_LONG, strategy="mean_reversion", price=100.0, confidence=0.8, reason=""
    )
    sig_normal.stop_loss = 95.0
    sig_normal.risk_multiplier = 1.0
    
    decision_normal = engine.risk.evaluate_entry(sig_normal, ctx, df)
    assert decision_normal.approved is True
    qty_normal = decision_normal.order.qty
    
    # 2. VIX unavailable state (risk_multiplier = 0.50)
    sig_defensive = Signal(
        symbol="SPY", action=SignalAction.ENTER_LONG, strategy="mean_reversion", price=100.0, confidence=0.8, reason=""
    )
    sig_defensive.stop_loss = 95.0
    sig_defensive.risk_multiplier = 0.50
    
    decision_defensive = engine.risk.evaluate_entry(sig_defensive, ctx, df)
    assert decision_defensive.approved is True
    qty_defensive = decision_defensive.order.qty
    
    # 3. Verify position sizing is reduced by ~50%
    assert qty_defensive == qty_normal // 2
