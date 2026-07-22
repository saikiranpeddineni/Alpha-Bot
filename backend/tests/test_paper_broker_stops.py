import pytest
from datetime import datetime, UTC
import pandas as pd

from app.brokers.paper import PaperBroker
from app.core.domain import OrderRequest, OrderSide, OrderType, OrderStatus


@pytest.mark.asyncio
async def test_paper_broker_sell_stop_rests_and_triggers():
    broker = PaperBroker(fill_market_on="immediate")
    broker.update_price("CRDO", 227.80, datetime.now(UTC))

    req = OrderRequest(
        symbol="CRDO",
        side=OrderSide.SELL,
        qty=89.0,
        order_type=OrderType.STOP,
        stop_price=225.18,
    )

    order = await broker.submit(req)

    # 1. Must be resting (not immediately filled at 227.80)
    assert order.status == OrderStatus.SUBMITTED
    assert order.stop_price == 225.18
    assert order.stop_loss is None
    assert order.id in broker._resting

    # 2. Advance bar where low does NOT reach stop (low = 226.00)
    bar_safe = pd.Series({"open": 227.80, "high": 228.00, "low": 226.00, "close": 227.00, "volume": 1000})
    broker.process_bar("CRDO", bar_safe, datetime.now(UTC))
    assert order.status == OrderStatus.SUBMITTED
    assert order.id in broker._resting

    # 3. Advance bar where low drops through stop (low = 224.50)
    bar_trigger = pd.Series({"open": 226.00, "high": 226.50, "low": 224.50, "close": 225.00, "volume": 1000})
    broker.process_bar("CRDO", bar_trigger, datetime.now(UTC))
    assert order.status == OrderStatus.FILLED
    assert order.id not in broker._resting
    assert order.filled_avg_price <= 225.18


@pytest.mark.asyncio
async def test_paper_broker_buy_stop_rests_and_triggers():
    broker = PaperBroker(fill_market_on="immediate")
    broker.update_price("XYZ", 150.00, datetime.now(UTC))

    req = OrderRequest(
        symbol="XYZ",
        side=OrderSide.BUY,
        qty=50.0,
        order_type=OrderType.STOP,
        stop_price=152.50,
    )

    order = await broker.submit(req)

    # 1. Must be resting (not immediately filled at 150.00)
    assert order.status == OrderStatus.SUBMITTED
    assert order.stop_price == 152.50
    assert order.id in broker._resting

    # 2. Advance bar where high reaches stop (high = 153.00)
    bar_trigger = pd.Series({"open": 150.50, "high": 153.00, "low": 150.00, "close": 152.80, "volume": 1000})
    broker.process_bar("XYZ", bar_trigger, datetime.now(UTC))
    assert order.status == OrderStatus.FILLED
    assert order.id not in broker._resting
    assert order.filled_avg_price >= 152.50


@pytest.mark.asyncio
async def test_paper_broker_stop_limit_trigger_and_limit():
    broker = PaperBroker(fill_market_on="immediate")
    broker.update_price("CRDO", 227.80, datetime.now(UTC))

    req = OrderRequest(
        symbol="CRDO",
        side=OrderSide.SELL,
        qty=100.0,
        order_type=OrderType.STOP_LIMIT,
        stop_price=225.00,
        limit_price=224.50,
    )

    order = await broker.submit(req)
    assert order.status == OrderStatus.SUBMITTED
    assert order.stop_price == 225.00
    assert order.limit_price == 224.50
    assert order.id in broker._resting

    # Triggered and filled at limit or better
    bar = pd.Series({"open": 226.00, "high": 226.00, "low": 224.00, "close": 224.80, "volume": 1000})
    broker.process_bar("CRDO", bar, datetime.now(UTC))
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price >= 224.50


@pytest.mark.asyncio
async def test_paper_broker_stop_limit_trigger_persists_across_bars():
    broker = PaperBroker(fill_market_on="immediate")

    broker.update_price(
        "XYZ",
        101.00,
        datetime.now(UTC),
    )

    req = OrderRequest(
        symbol="XYZ",
        side=OrderSide.SELL,
        qty=100.0,
        order_type=OrderType.STOP_LIMIT,
        stop_price=100.00,
        limit_price=99.00,
    )

    order = await broker.submit(req)

    assert order.status == OrderStatus.SUBMITTED
    assert order.stop_triggered is False

    # BAR 1: Stop crosses 100 (low=98.00), but price gaps below 99 limit (high=98.80) and never reaches 99.
    # Result: STOP triggers, LIMIT remains unfilled.
    trigger_only_bar = pd.Series({
        "open": 98.50,
        "high": 98.80,
        "low": 98.00,
        "close": 98.50,
        "volume": 1000,
    })

    broker.process_bar(
        "XYZ",
        trigger_only_bar,
        datetime.now(UTC),
    )

    assert order.stop_triggered is True
    assert order.status == OrderStatus.SUBMITTED
    assert order.id in broker._resting

    # BAR 2: Price is entirely ABOVE the original stop. The stop does NOT cross again.
    # But because the order was already triggered, it is now simply a SELL LIMIT @ 99.
    limit_fill_bar = pd.Series({
        "open": 100.50,
        "high": 101.00,
        "low": 100.25,
        "close": 100.75,
        "volume": 1000,
    })

    broker.process_bar(
        "XYZ",
        limit_fill_bar,
        datetime.now(UTC),
    )

    assert order.stop_triggered is True
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price >= 99.00
    assert order.id not in broker._resting
