import pytest
import asyncio
import pandas as pd
import json
from datetime import datetime, UTC, timedelta
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock

from app.config import Settings
from app.main import create_app
from app.core.domain import EngineState, Signal, SignalAction
from app.core.engine import TradingEngine


def make_vix_df(prices, ts_start=None):
    if ts_start is None:
        ts_start = datetime.now(UTC) - timedelta(minutes=len(prices))
    # Truncate microseconds to avoid datetime alignment mismatches
    ts_start = ts_start.replace(microsecond=0)
    indices = [ts_start + timedelta(minutes=i) for i in range(len(prices))]
    return pd.DataFrame(
        index=indices,
        data={
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [100.0] * len(prices)
        }
    )


def setup_healthy_mocks(engine):
    engine._force_gated = False
    engine.reconciliation_failed = False
    engine.reconciliation_mismatch = False
    engine.system_health = "NORMAL"
    engine.allow_new_entries = True
    engine.vix_unavailable = False
    engine.pending_reconciliation_orders = {}
    engine.portfolio._lots.clear()
    engine._last_scan_date = datetime.now(UTC).date()
    
    # Mock VIX frame to be fresh and normal (VIX=15)
    vix_df = make_vix_df([15.0] * 50)
    
    # Mock SPY frame to be healthy/bullish (above 200-period SMA)
    spy_prices = [100.0] * 210
    spy_df = make_vix_df(spy_prices)
    
    # Default mock data frame mapping
    def mock_frame(symbol, timeframe="1Min", limit=None):
        if symbol.upper() in ("VIX", "^VIX"):
            return vix_df
        elif symbol.upper() == "SPY":
            return spy_df
        else:
            return make_vix_df([50.0] * 50)
            
    engine.data.frame = MagicMock(side_effect=mock_frame)
    engine.data.last_price = MagicMock(side_effect=lambda sym: 15.0 if sym.upper() in ("VIX", "^VIX") else 100.0 if sym.upper() == "SPY" else 50.0)

    class MockAccount:
        buying_power = 200000.0
        equity = 100000.0
        cash = 100000.0
        
    async def mock_account():
        return MockAccount()
    engine.broker.account = mock_account
    
    async def mock_positions():
        return []
    engine.broker.positions = mock_positions


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_safety.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    
    original_run_loop = TradingEngine._run_loop
    TradingEngine._run_loop = AsyncMock()
    
    app = create_app(settings)
    
    with TestClient(app) as c:
        c.post("/api/engine/start", json={"mode": "demo", "confirm": "start"})
        yield c
        
    TradingEngine._run_loop = original_run_loop


def test_market_risk_vix_tiers(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # 1. Normal state: VIX = 15.0
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        assert engine.last_safety_decision.market_risk_state == "NORMAL"
        assert engine.last_safety_decision.risk_multiplier == 1.0
        assert engine.last_safety_decision.allow_new_entries is True
        
        # 2. Caution state: VIX = 18.0
        vix_df_caution = make_vix_df([18.0] * 50)
        engine.data.frame = MagicMock(side_effect=lambda sym, *args, **kwargs: vix_df_caution if sym.upper() in ("VIX", "^VIX") else make_vix_df([100.0]*210))
        engine.data.last_price = MagicMock(return_value=18.0)
        
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        assert engine.last_safety_decision.market_risk_state == "CAUTION"
        assert engine.last_safety_decision.risk_multiplier == 0.75
        assert engine.last_safety_decision.allow_new_entries is True
        
        # 3. Defensive state: VIX = 25.0
        vix_df_defensive = make_vix_df([25.0] * 50)
        engine.data.frame = MagicMock(side_effect=lambda sym, *args, **kwargs: vix_df_defensive if sym.upper() in ("VIX", "^VIX") else make_vix_df([100.0]*210))
        engine.data.last_price = MagicMock(return_value=25.0)
        
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        assert engine.last_safety_decision.market_risk_state == "DEFENSIVE"
        assert engine.last_safety_decision.risk_multiplier == 0.50
        assert engine.last_safety_decision.allow_new_entries is True
        
        # 4. Survival state: VIX = 35.0
        vix_df_survival = make_vix_df([35.0] * 50)
        engine.data.frame = MagicMock(side_effect=lambda sym, *args, **kwargs: vix_df_survival if sym.upper() in ("VIX", "^VIX") else make_vix_df([100.0]*210))
        engine.data.last_price = MagicMock(return_value=35.0)
        
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        assert engine.last_safety_decision.market_risk_state == "SURVIVAL"
        assert engine.last_safety_decision.risk_multiplier == 0.0
        assert engine.last_safety_decision.allow_new_entries is False
        
        # 5. VIX unavailable fall back to DEFENSIVE, system should NOT be HALTED
        engine.data.frame = MagicMock(return_value=pd.DataFrame())
        engine.data.last_price = MagicMock(return_value=None)
        
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        assert engine.last_safety_decision.market_risk_state == "DEFENSIVE"
        assert engine.system_health != "HALTED"
    finally:
        loop.close()


def test_market_risk_broad_market_trend(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Mock SPY price (90) to be below its 200-period SMA (100) -> BEARISH trend
    spy_prices = [100.0] * 199 + [90.0]
    spy_df = make_vix_df(spy_prices)
    
    engine.data.frame = MagicMock(side_effect=lambda sym, *args, **kwargs: spy_df if sym.upper() == "SPY" else make_vix_df([15.0]*50))
    engine.data.last_price = MagicMock(side_effect=lambda sym: 90.0 if sym.upper() == "SPY" else 15.0)
    
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        # Bearish trend shifts risk state to DEFENSIVE
        assert engine.last_safety_decision.market_risk_state == "DEFENSIVE"
        assert engine.last_safety_decision.risk_multiplier == 0.50
    finally:
        loop.close()


def test_behavioral_safety_reentry_after_stop(client):
    db = client.app.state.db
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # 1. Insert a stopped trade in the last 10 minutes
    now_ts = datetime.now(UTC)
    trade_payload = {
        "id": "trade_stop_test",
        "symbol": "AAPL",
        "strategy": "momentum",
        "entry_price": 150.0,
        "qty": 10,
        "pnl": -50.0,
        "exit_reason": "stop_loss",
        "sl_hit": True,
        "exit_at": (now_ts - timedelta(minutes=5)).isoformat()
    }
    with db._session() as s:
        from app.db.database import TradeRow
        s.add(TradeRow(
            id="trade_stop_test",
            symbol="AAPL",
            pnl=-50.0,
            exit_at=now_ts - timedelta(minutes=5),
            mode="demo",
            payload=json.dumps(trade_payload)
        ))
        s.commit()
        
    loop = asyncio.new_event_loop()
    try:
        sig_aapl = Signal(
            symbol="AAPL",
            strategy="momentum",
            action=SignalAction.ENTER_LONG,
            confidence=0.8,
            reason="test_reentry",
            price=150.0,
            stop_loss=145.0
        )
        object.__setattr__(sig_aapl, "policy_id", "test_policy")
        
        reason = loop.run_until_complete(engine._check_behavioral_safety(sig_aapl, now_ts))
        assert "re-entry on same symbol AAPL within 30 mins after stop" in reason
        
        # Test that a different symbol is NOT blocked
        sig_spy = Signal(
            symbol="SPY",
            strategy="momentum",
            action=SignalAction.ENTER_LONG,
            confidence=0.8,
            reason="test_reentry",
            price=150.0,
            stop_loss=145.0
        )
        object.__setattr__(sig_spy, "policy_id", "test_policy")
        
        reason_spy = loop.run_until_complete(engine._check_behavioral_safety(sig_spy, now_ts))
        assert reason_spy is None
    finally:
        loop.close()


def test_behavioral_safety_three_consecutive_losses(client):
    db = client.app.state.db
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    now_ts = datetime.now(UTC)
    with db._session() as s:
        from app.db.database import TradeRow
        for i in range(3):
            trade_payload = {
                "id": f"loss_trade_{i}",
                "symbol": "MSFT",
                "strategy": "momentum",
                "entry_price": 200.0,
                "qty": 5,
                "pnl": -50.0,
                "exit_reason": "take_profit",
                "sl_hit": False,
                "exit_at": (now_ts - timedelta(hours=i+1)).isoformat()
            }
            s.add(TradeRow(
                id=f"loss_trade_{i}",
                symbol="MSFT",
                pnl=-50.0,
                exit_at=now_ts - timedelta(hours=i+1),
                mode="demo",
                payload=json.dumps(trade_payload)
            ))
        s.commit()
        
    loop = asyncio.new_event_loop()
    try:
        sig = Signal(
            symbol="MSFT",
            strategy="momentum",
            action=SignalAction.ENTER_LONG,
            confidence=0.8,
            reason="test_loss",
            price=200.0,
            stop_loss=190.0
        )
        object.__setattr__(sig, "policy_id", "test_policy")
        
        reason = loop.run_until_complete(engine._check_behavioral_safety(sig, now_ts))
        assert "3 consecutive losses on symbol MSFT" in reason
    finally:
        loop.close()


def test_behavioral_safety_churn_turnover(client):
    db = client.app.state.db
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Current equity is 100k. Create high turnover (e.g. 250k volume in last 24h)
    now_ts = datetime.now(UTC)
    trade_payload = {
        "id": "churn_trade_1",
        "symbol": "AMZN",
        "strategy": "momentum",
        "entry_price": 100.0,
        "qty": 2500, # 250k notional volume
        "pnl": 2500.0,
        "exit_reason": "take_profit",
        "sl_hit": False,
        "exit_at": (now_ts - timedelta(hours=4)).isoformat()
    }
    with db._session() as s:
        from app.db.database import TradeRow
        s.add(TradeRow(
            id="churn_trade_1",
            symbol="AMZN",
            pnl=2500.0,
            exit_at=now_ts - timedelta(hours=4),
            mode="demo",
            payload=json.dumps(trade_payload)
        ))
        s.commit()
        
    loop = asyncio.new_event_loop()
    try:
        sig = Signal(
            symbol="AMZN",
            strategy="momentum",
            action=SignalAction.ENTER_LONG,
            confidence=0.8,
            reason="test_churn",
            price=100.0,
            stop_loss=95.0
        )
        object.__setattr__(sig, "policy_id", "test_policy")
        
        reason = loop.run_until_complete(engine._check_behavioral_safety(sig, now_ts))
        assert "high portfolio churn/turnover" in reason
    finally:
        loop.close()


def test_behavioral_safety_duplicate_and_pending_orders(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Mock a pending order for TSLA
    engine.pending_reconciliation_orders["order_id_123"] = {
        "symbol": "TSLA",
        "client_order_id": "order_id_123"
    }
    
    loop = asyncio.new_event_loop()
    try:
        sig = Signal(
            symbol="TSLA",
            strategy="momentum",
            action=SignalAction.ENTER_LONG,
            confidence=0.8,
            reason="test_dup",
            price=200.0,
            stop_loss=190.0
        )
        object.__setattr__(sig, "policy_id", "test_policy")
        
        reason = loop.run_until_complete(engine._check_behavioral_safety(sig, datetime.now(UTC)))
        assert "duplicate pending order for symbol TSLA" in reason
    finally:
        loop.close()


def test_behavioral_safety_correlation_clustering(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # AAPL is currently an open position in portfolio lots
    from types import SimpleNamespace
    engine.portfolio._lots["AAPL"] = SimpleNamespace(qty=10, entry_price=100.0)
    
    # Mock QQQ price frame to be perfectly correlated (same pattern) with AAPL
    t_start = datetime.now(UTC)
    aapl_df = make_vix_df([10.0, 11.0, 12.0, 13.0, 14.0, 15.0], ts_start=t_start)
    qqq_df = make_vix_df([10.0, 11.0, 12.0, 13.0, 14.0, 15.0], ts_start=t_start)
    
    engine.data.frame = MagicMock(side_effect=lambda sym, *args, **kwargs: aapl_df if sym.upper() == "AAPL" else qqq_df if sym.upper() == "QQQ" else make_vix_df([15.0]*50))
    
    loop = asyncio.new_event_loop()
    try:
        sig = Signal(
            symbol="QQQ",
            strategy="momentum",
            action=SignalAction.ENTER_LONG,
            confidence=0.8,
            reason="test_corr",
            price=200.0,
            stop_loss=190.0
        )
        object.__setattr__(sig, "policy_id", "test_policy")
        
        reason = loop.run_until_complete(engine._check_behavioral_safety(sig, datetime.now(UTC)))
        assert "high correlation (1.00) with open symbol AAPL" in reason
    finally:
        loop.close()


def test_reset_health_cannot_bypass_unresolved_issue(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Persistent critical safety failure: database write throws exception
    engine.db.set_setting = MagicMock(side_effect=Exception("Database read-only"))
    
    # Run heartbeat -> escalates to HALTED
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        assert engine.system_health == "HALTED"
        
        # Call manual reset-health route
        r = client.post("/api/system/reset-health")
        assert r.status_code == 200
        # The returned health state MUST still be HALTED because database write is still failing!
        assert r.json()["system_health"] == "HALTED"
    finally:
        loop.close()


def test_allow_new_entries_cannot_override_hard_halt(client):
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    
    # Trip hard halt
    engine.risk.hard_halted = True
    engine.risk.hard_halt_reason = "drawdown_limit"
    engine.system_health = "HALTED"
    
    # Call manual override to ALLOW entries
    r = client.post("/api/system/allow-new-entries", json={"allow": True})
    assert r.status_code == 200
    assert r.json()["allow_new_entries"] is False  # Manual override to allow entries cannot override hard halt!
    
    # Perform heartbeat check
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        # SafetyDecision MUST still block entries due to the hard halt state!
        assert engine.last_safety_decision.allow_new_entries is False
    finally:
        loop.close()


def test_verify_broker_protective_stops_undersized_and_take_profit_preservation(client):
    """Verify under-sized stop is canceled/replaced while take-profit limit order is preserved."""
    from app.core.domain import Position, Order, OrderSide, OrderType, OrderStatus
    engine = client.app.state.engine
    engine.risk_config.bypass_rule41 = False
    setup_healthy_mocks(engine)

    pos_long = Position(symbol="MU", qty=21.0, avg_entry_price=100.0, market_price=100.0)

    # 1. Under-sized stop order (19 shares @ 98.0)
    undersized_stop = Order(
        id="stop_19", client_order_id="stop_19", symbol="MU",
        side=OrderSide.SELL, qty=19.0, order_type=OrderType.STOP,
        stop_price=98.0, stop_loss=98.0, status=OrderStatus.SUBMITTED
    )
    # 2. Take-profit BRACKET LIMIT order above entry (21 shares @ 105.0)
    take_profit_limit = Order(
        id="tp_21", client_order_id="tp_21", symbol="MU",
        side=OrderSide.SELL, qty=21.0, order_type=OrderType.LIMIT,
        limit_price=105.0, status=OrderStatus.SUBMITTED, leg="tp", order_class="bracket"
    )

    # Cycle 1: Undersized stop cancelled, submit suppressed until cancel confirmed
    engine.broker.open_orders = AsyncMock(return_value=[undersized_stop, take_profit_limit])
    engine.broker.cancel = AsyncMock(return_value=True)
    engine.broker.submit = AsyncMock(return_value=Order(
        id="new_stop_21", client_order_id="new_stop_21", symbol="MU",
        side=OrderSide.SELL, qty=21.0, order_type=OrderType.STOP,
        stop_price=98.5, status=OrderStatus.SUBMITTED
    ))

    loop = asyncio.new_event_loop()
    try:
        status_c1 = loop.run_until_complete(engine.verify_broker_protective_stops([pos_long]))
        # 1. Undersized stop MUST be canceled
        engine.broker.cancel.assert_called_once_with("stop_19")
        # 2. Take-profit bracket limit MUST NOT be canceled (only 1 cancel call total)
        assert engine.broker.cancel.call_count == 1
        # 3. On cycle 1 while cancellation is pending, submit MUST NOT be called immediately
        engine.broker.submit.assert_not_called()
        assert status_c1["MU"] is False

        # Cycle 2: Cancellation confirmed (open_orders only has tp_21)
        engine.broker.open_orders = AsyncMock(return_value=[take_profit_limit])
        status_c2 = loop.run_until_complete(engine.verify_broker_protective_stops([pos_long]))
        engine.broker.submit.assert_called_once()
        req = engine.broker.submit.call_args[0][0]
        assert req.qty == 21.0
        assert req.symbol == "MU"
    finally:
        loop.close()


def test_verify_broker_protective_stops_cancellation_failure_aborts_submit(client):
    """Verify that if canceling an under-sized stop fails, recovery submit is safely aborted."""
    from app.core.domain import Position, Order, OrderSide, OrderType, OrderStatus
    engine = client.app.state.engine
    engine.risk_config.bypass_rule41 = False
    setup_healthy_mocks(engine)

    pos_long = Position(symbol="MU", qty=21.0, avg_entry_price=100.0, market_price=100.0)
    undersized_stop = Order(
        id="stop_19", client_order_id="stop_19", symbol="MU",
        side=OrderSide.SELL, qty=19.0, order_type=OrderType.STOP,
        stop_price=98.0, stop_loss=98.0, status=OrderStatus.SUBMITTED
    )

    engine.broker.open_orders = AsyncMock(return_value=[undersized_stop])
    engine.broker.cancel = AsyncMock(side_effect=Exception("Broker network timeout"))
    engine.broker.submit = AsyncMock()

    loop = asyncio.new_event_loop()
    try:
        status = loop.run_until_complete(engine.verify_broker_protective_stops([pos_long]))
        # Cancel attempted
        engine.broker.cancel.assert_called_once_with("stop_19")
        # Submit MUST BE ABORTED to prevent redundant held_for_orders collision loops!
        engine.broker.submit.assert_not_called()
        assert status["MU"] is False
    finally:
        loop.close()


def test_verify_broker_protective_stops_short_position_symmetry(client):
    """Verify protective stop recovery for SHORT positions (BUY stop above entry)."""
    from app.core.domain import Position, Order, OrderSide, OrderType, OrderStatus
    engine = client.app.state.engine
    engine.risk_config.bypass_rule41 = False
    setup_healthy_mocks(engine)

    pos_short = Position(symbol="AAPL", qty=-15.0, avg_entry_price=150.0, market_price=150.0)
    undersized_short_stop = Order(
        id="stop_short_10", client_order_id="stop_short_10", symbol="AAPL",
        side=OrderSide.BUY, qty=10.0, order_type=OrderType.STOP,
        stop_price=152.0, stop_loss=152.0, status=OrderStatus.SUBMITTED
    )

    engine.broker.open_orders = AsyncMock(return_value=[undersized_short_stop])
    engine.broker.cancel = AsyncMock(return_value=True)
    engine.broker.submit = AsyncMock(return_value=Order(
        id="new_short_stop_15", client_order_id="new_short_stop_15", symbol="AAPL",
        side=OrderSide.BUY, qty=15.0, order_type=OrderType.STOP,
        stop_price=152.25, status=OrderStatus.SUBMITTED
    ))

    loop = asyncio.new_event_loop()
    try:
        # Cycle 1: Cancel requested, submit suppressed
        status_c1 = loop.run_until_complete(engine.verify_broker_protective_stops([pos_short]))
        engine.broker.cancel.assert_called_once_with("stop_short_10")
        engine.broker.submit.assert_not_called()
        assert status_c1["AAPL"] is False

        # Cycle 2: Cancellation confirmed
        engine.broker.open_orders = AsyncMock(return_value=[])
        status_c2 = loop.run_until_complete(engine.verify_broker_protective_stops([pos_short]))
        engine.broker.submit.assert_called_once()
        req = engine.broker.submit.call_args[0][0]
        assert req.qty == 15.0
        assert req.side == OrderSide.BUY
        assert req.symbol == "AAPL"
    finally:
        loop.close()
