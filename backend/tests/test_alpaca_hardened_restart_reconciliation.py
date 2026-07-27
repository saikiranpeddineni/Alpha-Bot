"""Explicit restart recovery and fill reconciliation unit tests for the hardened Alpaca broker adapter."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.brokers import alpaca as legacy
from app.brokers.alpaca_hardened import AlpacaBroker
from app.brokers.base import BrokerTimeoutError, Fill
from app.core.domain import OrderStatus, OrderSide, OrderType
from app.db.database import Database


def _make_hardened_broker(monkeypatch):
    """Instantiate AlpacaBroker without network calls."""
    broker = object.__new__(AlpacaBroker)
    broker.is_paper = True
    broker._client = SimpleNamespace(
        get_account=AsyncMock(),
        get_asset=AsyncMock(),
        get_all_positions=AsyncMock(),
        get_orders=AsyncMock(),
        cancel_orders=AsyncMock(),
    )
    broker._fills = []
    broker._emitted_fill_ids = set()
    broker._processed_fill_qty = {}
    broker._processed_fill_notional = {}
    broker._emitted_fill_qty = {}
    broker._emitted_fill_notional = {}
    broker._meta = {}
    broker._symbol_strategies = {}
    broker._shortable_cache = {}
    broker._shorting_enabled = True
    broker.account_id = "ACCT12345"
    return broker


@pytest.mark.asyncio
async def test_hardened_broker_restart_during_pending_order(monkeypatch, tmp_path):
    """Pending/submitted orders on restart remain tracked as non-terminal SUBMITTED orders."""
    db = Database(tmp_path / "test_restart.db")
    monkeypatch.setattr(Database, "active", lambda: db)
    
    broker = _make_hardened_broker(monkeypatch)
    raw_order = SimpleNamespace(
        id="ord_pending_1",
        client_order_id="client_p1",
        symbol="AAPL",
        side="buy",
        qty="10",
        filled_qty="0",
        filled_avg_price=None,
        type="limit",
        status="pending_new",  # Alpaca non-terminal state
        submitted_at=datetime.now(UTC),
        filled_at=None,
        stop_price=None,
        limit_price=150.0,
        order_class=None,
        legs=None,
    )

    async def fake_call(_fn, *args, **_kwargs):
        return [raw_order]

    monkeypatch.setattr(legacy, "_call", fake_call)
    
    await broker.poll_fills()
    drained = broker.drain_fills()
    assert len(drained) == 0, "Pending order with 0 filled_qty must emit 0 fills"
    
    order = broker._to_order(raw_order)
    assert order.status == OrderStatus.SUBMITTED, "pending_new must map to non-terminal OrderStatus.SUBMITTED"


@pytest.mark.asyncio
async def test_hardened_broker_restart_after_partial_fill(monkeypatch, tmp_path):
    """Restart after partial fill emits exact remaining fill delta and updates watermark."""
    db = Database(tmp_path / "test_restart.db")
    monkeypatch.setattr(Database, "active", lambda: db)

    broker = _make_hardened_broker(monkeypatch)
    raw_order = SimpleNamespace(
        id="ord_partial_1",
        client_order_id="client_part1",
        symbol="MSFT",
        side="buy",
        qty="100",
        filled_qty="40",  # 40 shares filled
        filled_avg_price="300.0",
        type="market",
        status="partially_filled",
        submitted_at=datetime.now(UTC) - timedelta(minutes=5),
        filled_at=datetime.now(UTC) - timedelta(minutes=4),
        stop_price=None,
        limit_price=None,
        order_class=None,
        legs=None,
    )

    async def fake_call(_fn, *args, **_kwargs):
        return [raw_order]

    monkeypatch.setattr(legacy, "_call", fake_call)
    
    await broker.poll_fills()
    drained = broker.drain_fills()
    assert len(drained) == 1
    assert drained[0].order_id == "ord_partial_1"
    assert drained[0].qty == 40.0
    assert drained[0].price == 300.0


@pytest.mark.asyncio
async def test_hardened_broker_restart_after_timeout_recovery(monkeypatch, tmp_path):
    """When poll_fills times out during restart, it logs failure and continues without crashing."""
    db = Database(tmp_path / "test_restart.db")
    monkeypatch.setattr(Database, "active", lambda: db)

    broker = _make_hardened_broker(monkeypatch)

    async def fake_timeout_call(*_args, **_kwargs):
        raise BrokerTimeoutError("Alpaca connection timeout")

    monkeypatch.setattr(legacy, "_call", fake_timeout_call)
    
    # Must not raise exception
    await broker.poll_fills()
    assert len(broker.drain_fills()) == 0


@pytest.mark.asyncio
async def test_hardened_broker_restart_after_cancel(monkeypatch, tmp_path):
    """Canceled or replaced orders on restart are mapped to terminal OrderStatus.CANCELED."""
    broker = _make_hardened_broker(monkeypatch)
    
    raw_canceled = SimpleNamespace(
        id="ord_canceled_1", client_order_id="c1", symbol="NVDA", side="sell", qty="10",
        filled_qty="0", status="canceled", type="market", stop_price=None, limit_price=None,
        order_class=None, legs=None, submitted_at=None, filled_at=None,
    )
    raw_replaced = SimpleNamespace(
        id="ord_replaced_1", client_order_id="c2", symbol="NVDA", side="sell", qty="10",
        filled_qty="0", status="replaced", type="market", stop_price=None, limit_price=None,
        order_class=None, legs=None, submitted_at=None, filled_at=None,
    )

    assert broker._to_order(raw_canceled).status == OrderStatus.CANCELED
    assert broker._to_order(raw_replaced).status == OrderStatus.CANCELED
