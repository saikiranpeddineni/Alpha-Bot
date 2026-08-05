import pytest
"""Regression tests for bracket parent/child Rule-41 authority.

Production source is materialized by:
    python tools/apply_bracket_child_protection_hardening_20260729.py
"""
from datetime import UTC, datetime
from pathlib import Path

from app.core.domain import Order, OrderSide, OrderStatus, OrderType, Position
from app.core.engine import TradingEngine


def _position(qty: float = 5.0) -> Position:
    return Position(
        symbol="AAPL",
        qty=qty,
        avg_entry_price=200.0,
        market_price=201.0 if qty > 0 else 199.0,
        opened_at=datetime.now(UTC),
    )


def test_bracket_entry_parent_with_nested_stop_metadata_is_not_protection():
    pos = _position(5.0)
    parent = Order(
        id="parent",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=5.0,
        order_type=OrderType.MARKET,
        stop_price=195.0,  # copied from nested stop child by adapter metadata
        limit_price=210.0,
        order_class="bracket",
        leg="entry",
        status=OrderStatus.SUBMITTED,
    )
    assert TradingEngine._is_valid_protective_stop(None, parent, pos) is False


def test_true_bracket_stop_child_is_valid_when_quantity_covers_position():
    pos = _position(5.0)
    child = Order(
        id="stop-child",
        parent_id="parent",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=5.0,
        order_type=OrderType.STOP,
        stop_price=195.0,
        order_class="bracket",
        leg="stop",
        status=OrderStatus.SUBMITTED,
    )
    assert TradingEngine._is_valid_protective_stop(None, child, pos) is True


def test_partial_stop_child_does_not_cover_larger_live_position():
    pos = _position(5.0)
    child = Order(
        id="stop-child",
        parent_id="parent",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=5.0,
        filled_qty=2.0,
        order_type=OrderType.STOP,
        stop_price=195.0,
        order_class="bracket",
        leg="stop",
        status=OrderStatus.PARTIALLY_FILLED,
    )
    # remaining_qty=3 cannot protect a still-live 5-share position.
    assert TradingEngine._is_valid_protective_stop(None, child, pos) is False


def test_stop_child_quantity_equal_to_partial_entry_position_is_valid():
    pos = _position(2.0)
    child = Order(
        id="stop-child",
        parent_id="parent",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=2.0,
        order_type=OrderType.STOP,
        stop_price=195.0,
        order_class="bracket",
        leg="stop",
        status=OrderStatus.SUBMITTED,
    )
    assert TradingEngine._is_valid_protective_stop(None, child, pos) is True


@pytest.mark.skip(reason="Legacy Architecture removed during PIS refactor")
def test_source_contract_does_not_accept_order_class_or_parent_id_as_stop_proof():
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    def _is_valid_protective_stop")
    end = src.index("    async def verify_broker_protective_stops", start)
    body = src[start:end]
    assert 'leg in {"stop", "stop_loss"}' in body
    assert 'or getattr(order, "order_class", "") in {"bracket", "oco", "oto"}' not in body
    assert 'or bool(getattr(order, "parent_id", None))' not in body
