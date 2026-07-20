from datetime import UTC, datetime
import pandas as pd
import pytest

from app.brokers.paper import PaperBroker
from app.core.domain import OrderRequest, OrderSide, OrderStatus, OrderType


@pytest.mark.asyncio
async def test_full_fill_deterministic_default():
    broker = PaperBroker(starting_cash=100000.0, slippage_bps=0.0)
    broker.set_time(datetime(2026, 7, 10, 10, 0, tzinfo=UTC))
    broker._last_price["AAPL"] = 150.0

    req = OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=100, order_type=OrderType.MARKET, strategy="orb", reason="test")
    order = await broker.submit(req)
    assert order.status == OrderStatus.SUBMITTED

    # Advance next bar open
    bar = pd.Series({"open": 150.0, "high": 152.0, "low": 149.0, "close": 151.0, "volume": 10000.0})
    broker.process_bar("AAPL", bar, datetime(2026, 7, 10, 10, 1, tzinfo=UTC))

    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 100.0
    positions = await broker.positions()
    assert len(positions) == 1
    assert positions[0].qty == 100.0


@pytest.mark.asyncio
async def test_partial_fill_volume_participation_limit():
    # Set max 10% volume participation
    broker = PaperBroker(starting_cash=100000.0, slippage_bps=0.0, max_volume_participation_pct=0.10)
    broker.set_time(datetime(2026, 7, 10, 10, 0, tzinfo=UTC))
    broker._last_price["NVDA"] = 200.0

    req = OrderRequest(symbol="NVDA", side=OrderSide.BUY, qty=100, order_type=OrderType.MARKET, strategy="momentum", reason="test")
    order = await broker.submit(req)

    # Bar 1: Volume = 500. Max fill = 10% * 500 = 50 shares.
    bar1 = pd.Series({"open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 500.0})
    broker.process_bar("NVDA", bar1, datetime(2026, 7, 10, 10, 1, tzinfo=UTC))

    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_qty == 50.0
    positions = await broker.positions()
    assert positions[0].qty == 50.0

    # Bar 2: Volume = 1000. Max fill = 10% * 1000 = 100 shares. Remaining needed = 50 shares.
    bar2 = pd.Series({"open": 201.0, "high": 203.0, "low": 200.0, "close": 202.0, "volume": 1000.0})
    broker.process_bar("NVDA", bar2, datetime(2026, 7, 10, 10, 2, tzinfo=UTC))

    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 100.0
    positions = await broker.positions()
    assert positions[0].qty == 100.0
