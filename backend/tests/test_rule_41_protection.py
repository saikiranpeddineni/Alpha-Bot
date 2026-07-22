import pytest
from datetime import datetime, UTC, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.core.domain import Order, OrderSide, OrderStatus, OrderType, Position
from app.core.engine import TradingEngine

@pytest.fixture
def mock_engine():
    engine = MagicMock(spec=TradingEngine)
    engine.broker = AsyncMock()
    engine.portfolio = MagicMock()
    engine.portfolio._lots = {}
    engine.allow_new_entries = True
    engine.system_health = "NORMAL"
    engine._pending_close_symbols = set()
    engine._flattening_symbols = set()
    
    # Bind real verify_broker_protective_stops method
    engine._reconcile_protection = TradingEngine.verify_broker_protective_stops.__get__(engine, TradingEngine)
    return engine

@pytest.mark.asyncio
async def test_41_standalone_tp_cancelled_before_stop_recovery(mock_engine):
    """Standalone TP (SELL @ $169.90 for SKHY @ $168.40) is identified and cancelled on cycle 1, blocking immediate submit."""
    pos = Position(symbol="SKHY", qty=121.0, avg_entry_price=168.40, market_price=168.40)
    tp_order = Order(
        id="order_tp_1",
        client_order_id="cid_tp_1",
        symbol="SKHY",
        side=OrderSide.SELL,
        qty=121.0,
        order_type=OrderType.LIMIT,
        limit_price=169.90,
        status=OrderStatus.SUBMITTED,
    )
    mock_engine.broker.open_orders.return_value = [tp_order]

    status = await mock_engine._reconcile_protection([pos])

    # Verified stop is not yet present
    assert status["SKHY"] is False
    assert mock_engine.system_health == "DEGRADED"
    assert mock_engine.allow_new_entries is False
    
    # Standalone TP should be cancelled
    mock_engine.broker.cancel.assert_called_once_with("order_tp_1")
    # Recovery stop should NOT be submitted on cycle 1 (waiting for cancel confirmation)
    mock_engine.broker.submit.assert_not_called()

@pytest.mark.asyncio
async def test_41_does_not_cancel_valid_protective_stop(mock_engine):
    """Valid protective stop (SELL @ $165.80 for SKHY @ $168.40) is verified and preserved."""
    pos = Position(symbol="SKHY", qty=121.0, avg_entry_price=168.40, market_price=168.40)
    stop_order = Order(
        id="order_stop_1",
        client_order_id="cid_stop_1",
        symbol="SKHY",
        side=OrderSide.SELL,
        qty=121.0,
        order_type=OrderType.LIMIT,
        limit_price=165.80,
        stop_loss=165.80,
        status=OrderStatus.SUBMITTED,
    )
    mock_engine.broker.open_orders.return_value = [stop_order]

    status = await mock_engine._reconcile_protection([pos])

    assert status["SKHY"] is True
    assert mock_engine.system_health == "NORMAL"
    assert mock_engine.allow_new_entries is True
    
    # No order should be cancelled or submitted
    mock_engine.broker.cancel.assert_not_called()
    mock_engine.broker.submit.assert_not_called()

@pytest.mark.asyncio
async def test_41_does_not_cancel_bracket_oco_protection(mock_engine):
    """Broker-managed bracket/OCO structure is recognized and NOT cancelled automatically."""
    pos = Position(symbol="AAPL", qty=50.0, avg_entry_price=150.0, market_price=150.0)
    bracket_order = Order(
        id="order_bracket_1",
        client_order_id="cid_bracket_1",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=50.0,
        order_type=OrderType.LIMIT,
        limit_price=155.0,
        status=OrderStatus.SUBMITTED,
        leg="tp",
        parent_id="parent_1",
    )

    mock_engine.broker.open_orders.return_value = [bracket_order]

    status = await mock_engine._reconcile_protection([pos])

    assert status["AAPL"] is False
    assert mock_engine.system_health == "DEGRADED"
    
    # Cancellation must be suppressed for bracket legs
    mock_engine.broker.cancel.assert_not_called()
    mock_engine.broker.submit.assert_not_called()

@pytest.mark.asyncio
async def test_41_unknown_manual_exit_not_auto_cancelled(mock_engine):
    """Unclassified / manual exit orders suppress auto-cancellation and require manual/broker reconciliation."""
    pos = Position(symbol="TSLA", qty=10.0, avg_entry_price=200.0, market_price=200.0)
    unknown_order = Order(
        id="order_unknown_1",
        client_order_id="cid_unknown_1",
        symbol="TSLA",
        side=OrderSide.SELL,
        qty=10.0,
        order_type=OrderType.MARKET,
        status=OrderStatus.SUBMITTED,
    )

    mock_engine.broker.open_orders.return_value = [unknown_order]

    status = await mock_engine._reconcile_protection([pos])

    assert status["TSLA"] is False
    assert mock_engine.system_health == "DEGRADED"
    
    # Auto-cancellation must be suppressed
    mock_engine.broker.cancel.assert_not_called()
    mock_engine.broker.submit.assert_not_called()

@pytest.mark.asyncio
async def test_41_waits_for_cancel_confirmation_before_recovery(mock_engine):
    """Cycle 2: Once broker confirms cancellation (open_orders is empty), recovery stop is submitted."""
    pos = Position(symbol="SKHY", qty=121.0, avg_entry_price=168.40, market_price=168.40)
    
    # Open orders is empty (cancellation confirmed)
    mock_engine.broker.open_orders.return_value = []
    rec_order = Order(
        id="order_rec_1",
        client_order_id="cid_rec_1",
        symbol="SKHY",
        side=OrderSide.SELL,
        qty=121.0,
        order_type=OrderType.LIMIT,
        limit_price=165.874,
        status=OrderStatus.SUBMITTED,
    )
    mock_engine.broker.submit.return_value = rec_order

    status = await mock_engine._reconcile_protection([pos])

    # Emergency stop submitted
    mock_engine.broker.submit.assert_called_once()
    req = mock_engine.broker.submit.call_args[0][0]
    assert req.symbol == "SKHY"
    assert req.qty == 121.0
    assert req.limit_price == 165.874

@pytest.mark.asyncio
async def test_41_partial_qty_reservation_reconciled_safely(mock_engine):
    """Undersized stop (SELL 50 shares for 100 share position) is cancelled for replacement on next cycle."""
    pos = Position(symbol="NVDA", qty=100.0, avg_entry_price=120.0, market_price=120.0)
    undersized_stop = Order(
        id="order_under_1",
        client_order_id="cid_under_1",
        symbol="NVDA",
        side=OrderSide.SELL,
        qty=50.0,
        order_type=OrderType.LIMIT,
        limit_price=118.0,
        stop_loss=118.0,
        status=OrderStatus.SUBMITTED,
    )
    mock_engine.broker.open_orders.return_value = [undersized_stop]

    status = await mock_engine._reconcile_protection([pos])

    assert status["NVDA"] is False
    # Undersized stop requested for cancellation
    mock_engine.broker.cancel.assert_called_once_with("order_under_1")
    # Immediate replacement suppressed until cancellation confirmed
    mock_engine.broker.submit.assert_not_called()
