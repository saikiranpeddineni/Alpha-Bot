import pytest
import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.domain import OrderRole, OrderSide, OrderType, OrderRequest, Signal, SignalAction
from app.brokers.base import Fill
from app.brokers.alpaca import AlpacaBroker
from app.core.engine import TradingEngine
from app.core.durable_engine import DurableTradingEngine
from app.pis.thesis_drift import ThesisDriftEvaluator
from app.pis.models import PositionObservation


class MockClient:
    def submit_order(self, req: Any, **kwargs):
        class MockResponse:
            id = "mock-123"
            client_order_id = getattr(req, "client_order_id", "client-123")
            symbol = req.symbol
            side = req.side.value if hasattr(req.side, "value") else str(req.side)
            qty = req.qty
            filled_qty = 0.0
            type = "market"
            status = "accepted"
            submitted_at = None
            filled_at = None
            parent_id = None
            parent_order_id = None
        return MockResponse()

class MockConfig:
    broker_mode = "paper"
    mode = "paper"
    gemini_api_key = "mock"
    anthropic_api_key = "mock"
    def watchlist_symbols(self): return []

@pytest.fixture
def alpaca_broker():
    broker = AlpacaBroker("mock_key", "mock_secret", paper=True)
    broker._client = MockClient()
    return broker

@pytest.mark.asyncio
async def test_invalid_long_stop_rejected_before_broker(alpaca_broker):
    """P0 #5: Directional Entry Validation."""
    req = OrderRequest(
        symbol="TEST",
        side=OrderSide.BUY,
        qty=1.0,
        order_type=OrderType.LIMIT,
        limit_price=100.0,
        stop_loss=105.0, # Stop loss > limit price for LONG is invalid!
    )
    req.role = OrderRole.ENTRY
    with pytest.raises(ValueError, match="Invalid LONG entry stop geometry"):
        await alpaca_broker.submit(req)

@pytest.mark.asyncio
async def test_invalid_short_stop_rejected_before_broker(alpaca_broker):
    """P0 #5: Directional Entry Validation."""
    req = OrderRequest(
        symbol="TEST",
        side=OrderSide.SELL,
        qty=1.0,
        order_type=OrderType.LIMIT,
        limit_price=100.0,
        stop_loss=95.0, # Stop loss < limit price for SHORT is invalid!
    )
    req.role = OrderRole.ENTRY
    with pytest.raises(ValueError, match="Invalid SHORT entry stop geometry"):
        await alpaca_broker.submit(req)

@pytest.mark.asyncio
async def test_missing_timestamp_remains_none(alpaca_broker):
    """P0 #8: Stop Synthesizing Timestamps."""
    req = OrderRequest(
        symbol="TEST",
        side=OrderSide.BUY,
        qty=1.0,
    )
    order = await alpaca_broker.submit(req)
    # The MockResponse has submitted_at = None
    assert order.submitted_at is None, "Broker should not synthesize datetime.now(UTC) if missing"

def test_fill_role_propagation():
    """P0 #1: Fix Fill.role propagation."""
    fill = Fill(
        order_id="123",
        symbol="TEST",
        side=OrderSide.BUY,
        qty=1.0,
        price=100.0,
        role=OrderRole.EXIT.value # Or OrderRole.EXIT
    )
    assert fill.role == "EXIT"

def test_durable_broker_fill_order_role():
    """P0 #2: Fix Durable Broker-fill Order.role."""
    db_mock = type("MockDB", (), {
        "get_order": lambda *args, **kwargs: None,
        "get_setting": lambda *args, **kwargs: None,
        "get_recent_orders": lambda *args, **kwargs: [],
        "get_pending_orders": lambda *args, **kwargs: [],
        "load_portfolio": lambda *args, **kwargs: None,
        "load_positions": lambda *args, **kwargs: [],
        "get_fill_recovery_cursor_timestamp": lambda *args, **kwargs: None,
        "save_order": lambda *args, **kwargs: None,
    })()
    from app.config import Settings
    engine = DurableTradingEngine(settings=Settings(mode="paper", gemini_api_key="mock", anthropic_api_key="mock"), db=db_mock)
    engine.db = db_mock
    fill = Fill(
        order_id="mock-123",
        symbol="TEST",
        side=OrderSide.SELL,
        qty=1.0,
        price=100.0,
        role=OrderRole.EXIT
    )
    engine._persist_observed_fill_order(fill)
    assert len(engine.recent_orders) == 1
    order = engine.recent_orders[0]
    assert order.role == OrderRole.EXIT
    assert order.leg == "exit"

def test_thesis_drift_early_candidate_no_name_error():
    """P0 #7: Fix ThesisDriftEvaluator undefined history/side."""
    evaluator = ThesisDriftEvaluator()
    obs1 = PositionObservation(
        timestamp=datetime.now(UTC),
        symbol="TEST",
        current_price=100.0,
        high=102.0,
        low=98.0,
        close=99.0,
        volume=1000,
        side=1.0,
        qty=1.0,
        entry_price=100.0,
        atr=1.0,
        initial_stop=95.0,
        risk_per_share=5.0,
        bar_mae_r=-0.65,
    )
    obs2 = PositionObservation(
        timestamp=datetime.now(UTC),
        symbol="TEST",
        current_price=98.5,
        high=99.5,
        low=97.0,
        close=98.5,
        volume=1200,
        side=1.0,
        qty=1.0,
        entry_price=100.0,
        atr=1.0,
        initial_stop=95.0,
        risk_per_share=5.0,
        bar_mae_r=-0.8,
    )
    
    # Should not raise NameError when history is provided
    result = evaluator.evaluate(
        obs=obs2,
        trough_r=-0.8,
        stagnation_bars=2,
        peak_r=0.1,
        history_len=2,
        history=[obs1, obs2]
    )
    assert result is not None
