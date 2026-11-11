import pytest
from unittest.mock import AsyncMock, MagicMock
from app.core.engine_evaluation import TradingEngineEvaluationMixin


@pytest.mark.asyncio
async def test_pis_stop_ratchet_rejected_on_timeout():
    class TestEngine(TradingEngineEvaluationMixin):
        def __init__(self):
            self.broker = MagicMock()

    engine = TestEngine()
    # Mock replace_exit_leg returning None (timeout / unconfirmed)
    engine.broker.replace_exit_leg = AsyncMock(return_value=None)
    mock_order = MagicMock(id="order_1", stop_price=95.0, status="new", symbol="AAPL")
    mock_order.side = "sell"
    mock_order.type = "stop"
    engine.broker.get_orders = AsyncMock(return_value=[mock_order])

    result = await engine._sync_broker_protective_stop("AAPL", 98.0)
    assert result is False, "Expected stop ratchet to fail when broker does not acknowledge"
