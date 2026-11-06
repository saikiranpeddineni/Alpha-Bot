import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace
from app.brokers.alpaca import AlpacaBroker

@pytest.mark.asyncio
async def test_alpaca_close_position_full_liquidation_on_split():
    """Verify that when a full exit or split mismatch occurs, AlpacaBroker.close_position
    authoritatively calls native self._client.close_position to guarantee 100% liquidation."""
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client = MagicMock()
    broker._client.get_orders = MagicMock(return_value=[])
    broker._client.cancel_order_by_id = MagicMock()
    broker._symbol_strategies = {}
    broker._meta = {}

    # Broker holds 324 shares (post-split), but caller requested 108
    mock_pos = SimpleNamespace(qty="-324.0", symbol="SOXX")
    broker._client.get_open_position = MagicMock(return_value=mock_pos)
    
    mock_order = MagicMock()
    mock_order.id = "order-full-close-123"
    broker._client.close_position = MagicMock(return_value=mock_order)
    broker._to_order = MagicMock(return_value=mock_order)

    # Calling with reason="Momentum faded (Close > VWAP)" and qty=108
    order = await broker.close_position("SOXX", reason="Momentum faded (Close > VWAP)", qty=108)

    # Must invoke authoritative native full close_position, NOT partial MarketOrderRequest
    broker._client.close_position.assert_called_once_with("SOXX")
    assert not hasattr(broker._client, "submit_order") or broker._client.submit_order.call_count == 0

@pytest.mark.asyncio
async def test_alpaca_close_position_partial_scale_out_preserved():
    """Verify that genuine partial scale-outs (e.g. scale_out_half) still submit partial market orders."""
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client = MagicMock()
    broker._client.get_orders = MagicMock(return_value=[])
    broker._client.cancel_order_by_id = MagicMock()
    broker._symbol_strategies = {}
    broker._meta = {}

    # Broker holds 100 shares, caller scaling out 50
    mock_pos = SimpleNamespace(qty="100.0", symbol="AAPL")
    broker._client.get_open_position = MagicMock(return_value=mock_pos)

    mock_order = MagicMock()
    mock_order.id = "order-partial-456"
    broker._client.submit_order = MagicMock(return_value=mock_order)
    broker._to_order = MagicMock(return_value=mock_order)

    order = await broker.close_position("AAPL", reason="scale_out_half", qty=50)

    # Must submit partial market order for 50 shares
    assert broker._client.submit_order.call_count == 1
    call_req = broker._client.submit_order.call_args[0][0]
    assert float(call_req.qty) == 50.0
    assert call_req.symbol == "AAPL"
