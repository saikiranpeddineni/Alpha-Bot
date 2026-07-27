from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace

import pytest

from app.core.domain import OrderSide


class _GetOrdersRequest:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _install_fake_alpaca(monkeypatch):
    alpaca = ModuleType("alpaca")
    trading = ModuleType("alpaca.trading")
    client = ModuleType("alpaca.trading.client")
    requests = ModuleType("alpaca.trading.requests")

    class TradingClient:
        def __init__(self, *args, **kwargs):
            pass

    client.TradingClient = TradingClient
    requests.GetOrdersRequest = _GetOrdersRequest
    monkeypatch.setitem(sys.modules, "alpaca", alpaca)
    monkeypatch.setitem(sys.modules, "alpaca.trading", trading)
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", client)
    monkeypatch.setitem(sys.modules, "alpaca.trading.requests", requests)


def _order(qty: float, status: str = "partially_filled", *, oid: str = "order-1", side: str = "buy"):
    return SimpleNamespace(
        id=oid,
        symbol="AAPL",
        side=side,
        status=status,
        filled_qty=qty,
        filled_avg_price=100.0 if qty else None,
        filled_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
    )


@pytest.fixture
def broker(monkeypatch):
    _install_fake_alpaca(monkeypatch)
    from app.brokers.alpaca import AlpacaBroker

    return AlpacaBroker("key", "secret", paper=True)


@pytest.mark.asyncio
async def test_cumulative_fill_snapshots_emit_only_new_deltas(broker):
    snapshots = [[_order(40)], [_order(40)], [_order(75)], [_order(100, "filled")]]
    broker._client.get_orders = lambda request: snapshots.pop(0)

    emitted = []
    for _ in range(4):
        await broker.poll_fills()
        emitted.extend(broker.drain_fills())

    assert [fill.qty for fill in emitted] == [40.0, 35.0, 25.0]
    assert sum(fill.qty for fill in emitted) == 100.0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["canceled", "expired"])
async def test_partial_then_terminal_order_keeps_executed_delta(broker, terminal_status):
    snapshots = [[_order(40)], [_order(40, terminal_status)]]
    broker._client.get_orders = lambda request: snapshots.pop(0)

    await broker.poll_fills()
    first = broker.drain_fills()
    await broker.poll_fills()
    second = broker.drain_fills()

    assert [fill.qty for fill in first] == [40.0]
    assert second == []


@pytest.mark.asyncio
async def test_stale_cumulative_snapshot_cannot_move_watermark_backward_or_duplicate(broker):
    snapshots = [[_order(75)], [_order(40)], [_order(75)], [_order(100, "filled")]]
    broker._client.get_orders = lambda request: snapshots.pop(0)

    emitted = []
    for _ in range(4):
        await broker.poll_fills()
        emitted.extend(broker.drain_fills())

    assert [fill.qty for fill in emitted] == [75.0, 25.0]
    # drain_fills only hands the executions to the engine; it must not claim
    # durable processing before PortfolioTracker ACKs them. The in-flight
    # watermark still advances monotonically and blocks stale duplicates.
    assert broker._emitted_fill_qty["order-1"] == 100.0
    assert "order-1" not in broker._processed_fill_qty


@pytest.mark.asyncio
async def test_short_partial_fills_use_same_delta_contract(broker):
    snapshots = [[_order(40, side="sell")], [_order(75, side="sell")]]
    broker._client.get_orders = lambda request: snapshots.pop(0)

    await broker.poll_fills()
    first = broker.drain_fills()
    await broker.poll_fills()
    second = broker.drain_fills()

    assert [fill.qty for fill in first + second] == [40.0, 35.0]
    assert all(fill.side == OrderSide.SELL for fill in first + second)


@pytest.mark.asyncio
async def test_orders_without_fill_price_do_not_advance_processed_watermark(broker):
    missing_price = _order(40)
    missing_price.filled_avg_price = None
    snapshots = [[missing_price], [_order(40)]]
    broker._client.get_orders = lambda request: snapshots.pop(0)

    await broker.poll_fills()
    assert broker.drain_fills() == []
    assert "order-1" not in broker._processed_fill_qty

    await broker.poll_fills()
    assert [fill.qty for fill in broker.drain_fills()] == [40.0]
