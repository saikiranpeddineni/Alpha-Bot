from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.brokers import alpaca as legacy
from app.brokers.alpaca_hardened import AlpacaBroker, _paginate_orders
from app.core.domain import OrderStatus, Position
from app.risk.manager import is_entry_order


def _broker_without_client_init():
    broker = object.__new__(AlpacaBroker)
    broker._shorting_enabled = None
    broker._shortable_cache = {}
    return broker


def _raw_order(
    order_id: str,
    *,
    order_type: str = "market",
    order_class: str | None = None,
    stop_price: float | None = None,
    limit_price: float | None = None,
    parent_id: str | None = None,
    parent_order_id: str | None = None,
    leg: str | None = None,
    legs: list | None = None,
    side: str = "buy",
):
    return SimpleNamespace(
        id=order_id,
        client_order_id=f"client-{order_id}",
        symbol="AAPL",
        side=side,
        qty="10",
        filled_qty="0",
        filled_avg_price=None,
        type=order_type,
        status="new",
        stop_price=stop_price,
        limit_price=limit_price,
        order_class=order_class,
        legs=legs,
        related_orders=None,
        parent_order_id=parent_order_id,
        parent_id=parent_id,
        leg=leg,
        submitted_at=None,
        filled_at=None,
    )


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


def test_to_order_classifies_oto_parent_with_stop_as_entry():
    """An OTO parent remains an entry after nested stop metadata is copied onto it."""
    broker = _broker_without_client_init()
    stop_child = _raw_order(
        "oto-stop-1",
        order_type="stop",
        order_class="oto",
        stop_price=95.0,
        parent_id="oto-parent-1",
        side="sell",
    )
    parent = _raw_order(
        "oto-parent-1",
        order_type="market",
        order_class="oto",
        legs=[stop_child],
    )

    order = broker._to_order(parent)

    assert order.leg == "entry"
    assert order.stop_price == 95.0
    assert is_entry_order(
        order,
        [Position(symbol="AAPL", qty=10.0, avg_entry_price=100.0, market_price=100.0)],
    ) is True


def test_to_order_classifies_oto_stop_child_as_stop():
    """An OTO child keeps its protective-stop identity from its parent identifier."""
    broker = _broker_without_client_init()
    child = _raw_order(
        "oto-stop-2",
        order_type="stop",
        order_class="oto",
        stop_price=95.0,
        parent_order_id="oto-parent-2",
        side="sell",
    )

    order = broker._to_order(child)

    assert order.leg == "stop"
    assert order.parent_id == "oto-parent-2"
    assert is_entry_order(
        order,
        [Position(symbol="AAPL", qty=10.0, avg_entry_price=100.0, market_price=100.0)],
    ) is False


def test_to_order_classifies_bracket_parent_and_children():
    """Bracket parent metadata is entry-owned while stop/target children remain exits."""
    broker = _broker_without_client_init()
    stop_child = _raw_order(
        "bracket-stop-1",
        order_type="stop",
        order_class="bracket",
        stop_price=95.0,
        parent_id="bracket-parent-1",
        side="sell",
    )
    target_child = _raw_order(
        "bracket-target-1",
        order_type="limit",
        order_class="bracket",
        limit_price=110.0,
        parent_id="bracket-parent-1",
        side="sell",
    )
    parent = _raw_order(
        "bracket-parent-1",
        order_type="market",
        order_class="bracket",
        legs=[stop_child, target_child],
    )
    bracket_parent_with_only_stop = _raw_order(
        "bracket-parent-stop-only",
        order_type="market",
        order_class="bracket",
        legs=[stop_child],
    )
    position = Position(symbol="AAPL", qty=10.0, avg_entry_price=100.0, market_price=100.0)

    parent_order = broker._to_order(parent)
    stop_only_parent_order = broker._to_order(bracket_parent_with_only_stop)
    stop_order = broker._to_order(stop_child)
    target_order = broker._to_order(target_child)

    assert parent_order.leg == "entry"
    assert parent_order.stop_price == 95.0
    assert parent_order.limit_price == 110.0
    assert stop_only_parent_order.leg == "entry"
    assert stop_only_parent_order.stop_price == 95.0
    assert stop_order.leg == "stop"
    assert target_order.leg == "take_profit"
    assert is_entry_order(parent_order, [position]) is True
    assert is_entry_order(stop_order, [position]) is False
    assert is_entry_order(target_order, [position]) is False


@pytest.mark.asyncio
async def test_open_orders_retains_nested_parent_identity_for_children(monkeypatch):
    """Nested SDK orders without explicit parent fields still classify by relation IDs."""
    broker = _broker_without_client_init()
    broker._client = SimpleNamespace(get_orders=lambda *_args, **_kwargs: None)
    stop_child = _raw_order(
        "nested-stop-1",
        order_type="stop",
        order_class="bracket",
        stop_price=95.0,
        side="sell",
    )
    target_child = _raw_order(
        "nested-target-1",
        order_type="limit",
        order_class="bracket",
        limit_price=110.0,
        side="sell",
    )
    parent = _raw_order(
        "nested-parent-1",
        order_type="market",
        order_class="bracket",
        legs=[stop_child, target_child],
    )

    async def fake_call(*_args, **_kwargs):
        return [parent]

    monkeypatch.setattr(legacy, "_call", fake_call)

    # open_orders imports the Alpaca request type, so only the broker call is
    # mocked; the nested parent/child traversal remains real.
    orders = await broker.open_orders()

    assert [order.id for order in orders] == [
        "nested-parent-1",
        "nested-stop-1",
        "nested-target-1",
    ]
    assert orders[0].leg == "entry"
    assert orders[1].leg == "stop"
    assert orders[1].parent_id == "nested-parent-1"
    assert orders[2].leg == "take_profit"
    assert orders[2].parent_id == "nested-parent-1"


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
