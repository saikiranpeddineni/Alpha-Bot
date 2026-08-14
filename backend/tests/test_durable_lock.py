import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.core.durable_engine import DurableTradingEngine
from app.core.domain import Order, OrderStatus, OrderRole
from app.config import Settings
import time

@pytest.mark.asyncio
async def test_partial_exit_does_not_block_later_full_exit():
    engine = DurableTradingEngine(
        settings=Settings(ALPHADESK_MODE="demo", DATA_PROVIDER="synthetic", ALPHADESK_DB_PATH=":memory:"),
        db=MagicMock()
    )
    engine.broker = AsyncMock()
    engine.portfolio = MagicMock()
    engine.risk = MagicMock()
    engine.insights = MagicMock()
    
    engine.broker.close_position = AsyncMock(return_value=Order(
        symbol="AAPL", side="sell", qty=50, order_type="market", status=OrderStatus.FILLED
    ))
    
    engine.portfolio._lots = {"AAPL": MagicMock(qty=100)}
    engine._flattening_symbols = set()
    
    partial_order = await engine._close_position_guarded(
        symbol="AAPL",
        reason="scale_out",
        qty=50,
        strategy="momentum",
        role=OrderRole.TAKE_PROFIT,
    )
    
    assert partial_order is not None
    
    partial_order.status = OrderStatus.FILLED
    engine.portfolio._lots["AAPL"].qty = 50
    
    full_order = await engine._close_position_guarded(
        symbol="AAPL",
        reason="confirmed_thesis_failure",
        qty=None,
        strategy="momentum",
        role=OrderRole.EXIT,
    )
    
    assert full_order is not None
