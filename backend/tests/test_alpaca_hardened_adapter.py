from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.brokers import alpaca as legacy
from app.brokers.alpaca_hardened import AlpacaBroker, _paginate_orders
from app.core.domain import OrderStatus


def _broker_without_client_init():
    broker = object.__new__(AlpacaBroker)
    broker._shorting_enabled = None
    broker._shortable_cache = {}
    return broker


@pytest.mark.asyncio
async def test_is_shortable_uses_borrow_status_and_account_permission(monkeypatch):
    broker = _broker_without_client_init()
    broker._client = SimpleNamespace(get_account=lambda: None, get_asset=lambda _s: None)
    responses = iter([
        SimpleNamespace(shorting_enabled=True),
        SimpleNamespace(shortable=True, borrow_status="easy_to_borrow", easy_to_borrow=False),
    ])

    async def fake_call(*_args, **_kwargs):
        return next(responses)

    monkeypatch.setattr(legacy, "_call", fake_call)
    assert await broker.is_shortable("AMZN") is True


@pytest.mark.asyncio
async def test_is_shortable_rejects_locate_required_asset(monkeypatch):
    broker = _broker_without_client_init()
    broker._client = SimpleNamespace(get_account=lambda: None, get_asset=lambda _s: None)
    responses = iter([
        SimpleNamespace(shorting_enabled=True),
        SimpleNamespace(shortable=True, borrow_status="hard_to_borrow", easy_to_borrow=True),
    ])

    async def fake_call(*_args, **_kwargs):
        return next(responses)

    monkeypatch.setattr(legacy, "_call", fake_call)
    assert await broker.is_shortable("AMZN") is False


@pytest.mark.asyncio
async def test_is_shortable_missing_account_permission_fails_closed(monkeypatch):
    broker = _broker_without_client_init()
    broker._client = SimpleNamespace(get_account=lambda: None)

    async def fake_call(*_args, **_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(legacy, "_call", fake_call)
    assert await broker.is_shortable("AMZN") is False


def test_to_order_keeps_done_for_day_nonterminal():
    broker = _broker_without_client_init()
    broker._meta = {}
    raw = SimpleNamespace(
        id="1", client_order_id="c1", symbol="AAPL", side="buy", qty="1",
        filled_qty="0", type="market", status="done_for_day", stop_price=None,
        limit_price=None, order_class=None, legs=None, parent_order_id=None,
        parent_id=None, submitted_at=None, filled_at=None,
    )
    order = broker._to_order(raw)
    assert order.status == OrderStatus.SUBMITTED


@pytest.mark.asyncio
async def test_paginate_orders_forces_ascending_and_uses_submitted_at_cursor(monkeypatch):
    calls = []
    first_ts = datetime(2026, 7, 1, tzinfo=UTC)
    second_ts = first_ts + timedelta(microseconds=1)
    third_ts = second_ts + timedelta(microseconds=1)
    page1 = [
        SimpleNamespace(id="1", submitted_at=first_ts),
        SimpleNamespace(id="2", submitted_at=second_ts),
    ]
    page2 = [SimpleNamespace(id="3", submitted_at=third_ts)]
    pages = iter([page1, page2])

    async def fake_call(_fn, request, **_kwargs):
        calls.append(request)
        return next(pages)

    monkeypatch.setattr(legacy, "_call", fake_call)
    request = SimpleNamespace(
        status="all", after=None, limit=2, symbols=None, side=None,
        nested=None, until=None,
    )
    result = await _paginate_orders(SimpleNamespace(get_orders=AsyncMock()), request)
    assert [o.id for o in result] == ["1", "2", "3"]
    assert calls[1].after == second_ts
