from unittest.mock import AsyncMock

import pytest

from app.brokers.alpaca_hardened import AlpacaBroker
from app.core.domain import Order, OrderSide, OrderStatus, OrderType


@pytest.mark.asyncio
async def test_update_protection_replaces_existing_stop_leg():
    broker = object.__new__(AlpacaBroker)
    existing = Order(
        id="stop-1",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=10,
        order_type=OrderType.STOP,
        stop_price=190.0,
        status=OrderStatus.SUBMITTED,
        parent_id="entry-1",
    )
    replacement = existing.model_copy(update={"stop_price": 192.0})
    broker.open_orders = AsyncMock(return_value=[existing])
    broker.replace_exit_leg = AsyncMock(return_value=replacement)
    broker.submit = AsyncMock()

    result = await broker.update_protection("AAPL", 192.0, OrderSide.BUY, 10)

    broker.replace_exit_leg.assert_awaited_once_with("stop-1", stop_price=192.0)
    broker.submit.assert_not_awaited()
    assert result is replacement
    assert result.stop_loss == 192.0


@pytest.mark.asyncio
async def test_update_protection_creates_stop_when_none_exists():
    broker = object.__new__(AlpacaBroker)
    broker.open_orders = AsyncMock(return_value=[])
    created = Order(
        id="stop-new",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=10,
        order_type=OrderType.STOP,
        stop_price=192.0,
        status=OrderStatus.SUBMITTED,
        strategy="emergency_protection",
    )
    broker.submit = AsyncMock(return_value=created)
    broker.replace_exit_leg = AsyncMock()

    result = await broker.update_protection("AAPL", 192.0, OrderSide.BUY, 10)

    broker.replace_exit_leg.assert_not_awaited()
    broker.submit.assert_awaited_once()
    request = broker.submit.await_args.args[0]
    assert request.symbol == "AAPL"
    assert request.side == OrderSide.SELL
    assert request.qty == 10
    assert request.order_type == OrderType.STOP
    assert request.stop_price == 192.0
    assert request.strategy == "emergency_protection"
    assert result is created
