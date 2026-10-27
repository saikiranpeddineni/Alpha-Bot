"""Paper broker fill-engine tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from app.brokers.paper import PaperBroker
from app.core.domain import OrderRequest, OrderSide, OrderStatus, OrderType

NOW = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)


def bar(o: float, h: float, low: float, c: float, v: float = 10_000) -> pd.Series:
    return pd.Series({"open": o, "high": h, "low": low, "close": c, "volume": v})


@pytest.fixture
def broker() -> PaperBroker:
    b = PaperBroker(
        starting_cash=100_000,
        slippage_bps=0.0,
        commission_per_share=0.0,
        fill_market_on="immediate",
        allow_margin=False,
    )
    b.update_price("AAPL", 100.0, NOW)
    return b


async def test_market_buy_fills_immediately(broker: PaperBroker):
    order = await broker.submit(OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10))
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == 100.0
    assert broker.cash == 100_000 - 1000
    positions = await broker.positions()
    assert len(positions) == 1 and positions[0].qty == 10


async def test_slippage_applied_against_buyer():
    b = PaperBroker(starting_cash=100_000, slippage_bps=10.0)  # 0.1%
    b.update_price("AAPL", 100.0, NOW)
    order = await b.submit(OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10))
    assert order.filled_avg_price == pytest.approx(100.1)


async def test_no_price_rejects(broker: PaperBroker):
    order = await broker.submit(OrderRequest(symbol="ZZZZ", side=OrderSide.BUY, qty=1))
    assert order.status == OrderStatus.REJECTED


async def test_insufficient_cash_rejects(broker: PaperBroker):
    order = await broker.submit(OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=2000))
    assert order.status == OrderStatus.REJECTED


async def test_limit_order_rests_then_fills(broker: PaperBroker):
    order = await broker.submit(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=98.0)
    )
    assert order.status == OrderStatus.SUBMITTED
    assert len(await broker.open_orders()) == 1
    broker.process_bar("AAPL", bar(99.0, 99.5, 97.5, 98.5), NOW)  # dips through the limit
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == 98.0  # filled at the limit, not the low


async def test_limit_gap_through_fills_at_open(broker: PaperBroker):
    order = await broker.submit(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=98.0)
    )
    broker.process_bar("AAPL", bar(95.0, 96.0, 94.0, 95.5), NOW)  # gaps below the limit
    assert order.filled_avg_price == 95.0  # better price honored


async def test_bracket_stop_hits_before_target_same_bar(broker: PaperBroker):
    await broker.submit(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, stop_loss=99.0, take_profit=102.0)
    )
    broker.drain_fills()
    # One wide bar touches both stop and target: conservative rule = stop first
    broker.process_bar("AAPL", bar(100.0, 103.0, 98.5, 101.0), NOW)
    assert not await broker.positions()
    fills = broker.drain_fills()
    assert len(fills) == 1
    assert fills[0].side == OrderSide.SELL
    assert fills[0].price == pytest.approx(99.0)
    assert fills[0].reason == "stop_loss"


async def test_bracket_target_fills(broker: PaperBroker):
    await broker.submit(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, stop_loss=98.0, take_profit=101.0)
    )
    broker.drain_fills()
    broker.process_bar("AAPL", bar(100.2, 101.5, 100.0, 101.2), NOW)
    fills = broker.drain_fills()
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(101.0)
    assert fills[0].reason == "take_profit"
    assert broker.cash == pytest.approx(100_000 + 10 * 1.0)


async def test_short_bracket_stop(broker: PaperBroker):
    await broker.submit(
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, stop_loss=101.0, take_profit=97.0)
    )
    positions = await broker.positions()
    assert positions[0].qty == -10
    broker.drain_fills()
    broker.process_bar("AAPL", bar(100.5, 101.5, 100.2, 101.2), NOW)  # rips through the stop
    assert not await broker.positions()
    fills = broker.drain_fills()
    assert fills[0].side == OrderSide.BUY and fills[0].reason == "stop_loss"


async def test_next_open_mode_queues_market_orders():
    b = PaperBroker(starting_cash=100_000, slippage_bps=0.0, fill_market_on="next_open")
    b.update_price("AAPL", 100.0, NOW)
    order = await b.submit(OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, strategy="automated"))
    assert order.status == OrderStatus.SUBMITTED  # queued, no look-ahead fill
    b.process_bar("AAPL", bar(100.7, 101.0, 100.4, 100.9), NOW)
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == pytest.approx(100.7)  # next bar's open


async def test_close_position_and_flatten(broker: PaperBroker):
    await broker.submit(OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10))
    broker.update_price("MSFT", 200.0, NOW)
    await broker.submit(OrderRequest(symbol="MSFT", side=OrderSide.BUY, qty=5))
    assert len(await broker.positions()) == 2
    await broker.close_position("AAPL", reason="test")
    assert len(await broker.positions()) == 1
    await broker.flatten_all()
    assert not await broker.positions()
    assert broker.cash == pytest.approx(100_000)  # zero slippage round trips


async def test_equity_tracks_unrealized(broker: PaperBroker):
    await broker.submit(OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=100))
    broker.process_bar("AAPL", bar(101.0, 102.0, 100.5, 102.0), NOW)
    account = await broker.account()
    assert account.equity == pytest.approx(100_000 + 100 * 2.0)
