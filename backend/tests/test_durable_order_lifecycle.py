from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.core.domain import Order, OrderSide, OrderStatus, OrderType
from app.core.durable_engine import DurableTradingEngine


def _fill(order_id: str = "order-1"):
    return SimpleNamespace(
        order_id=order_id,
        symbol="SPY",
        side=OrderSide.SELL,
        qty=2.0,
        price=501.25,
        strategy="momentum",
        reason="take profit",
        ts=datetime(2026, 7, 27, 19, 30, tzinfo=UTC),
        cumulative_qty=2.0,
    )


def test_observed_fill_updates_existing_order_to_terminal_state():
    saved = []
    existing = Order(
        id="order-1",
        client_order_id="client-1",
        symbol="SPY",
        side=OrderSide.SELL,
        qty=2.0,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
    )
    engine = object.__new__(DurableTradingEngine)
    engine.mode = "paper"
    engine.recent_orders = __import__("collections").deque([existing], maxlen=200)
    engine.db = SimpleNamespace(
        get_order=lambda _order_id: None,
        save_order=lambda order, mode: saved.append((order, mode)),
    )

    engine._persist_observed_fill_order(_fill())

    assert existing.status == OrderStatus.FILLED
    assert existing.filled_qty == 2.0
    assert existing.filled_avg_price == 501.25
    assert existing.filled_at is not None
    assert saved[-1] == (existing, "paper")


def test_broker_driven_fill_creates_missing_durable_order_row():
    saved = []
    engine = object.__new__(DurableTradingEngine)
    engine.mode = "paper"
    engine.recent_orders = __import__("collections").deque(maxlen=200)
    engine.db = SimpleNamespace(
        get_order=lambda _order_id: None,
        save_order=lambda order, mode: saved.append((order, mode)),
    )

    engine._persist_observed_fill_order(_fill("broker-close-9"))

    order, mode = saved[-1]
    assert mode == "paper"
    assert order.id == "broker-close-9"
    assert order.status == OrderStatus.FILLED
    assert order.leg == "exit"
    assert order.reason == "take profit"
    assert order.filled_qty == 2.0
    assert engine.recent_orders[0].id == "broker-close-9"
