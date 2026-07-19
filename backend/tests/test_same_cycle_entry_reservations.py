from __future__ import annotations

from app.core.domain import Order, OrderSide, OrderStatus, OrderType, Position
from app.core.entry_reservations import SameCycleEntryReservations


def _order(symbol: str, side: OrderSide, qty: float, status: OrderStatus) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        status=status,
        strategy="reservation-test",
    )


def test_active_long_submission_reserves_cash_and_position_capacity():
    ledger = SameCycleEntryReservations.from_snapshot(100.0, [])
    assert ledger.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.SUBMITTED), 10.0)
    assert ledger.cash == 20.0
    assert [p.symbol for p in ledger.positions] == ["AAA"]
    assert ledger.positions[0].qty == 8


def test_second_candidate_sees_prior_same_cycle_reservation():
    existing = Position(symbol="OLD", qty=1, avg_entry_price=50, market_price=50)
    ledger = SameCycleEntryReservations.from_snapshot(100.0, [existing])
    ledger.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.SUBMITTED), 10.0)
    assert ledger.cash == 20.0
    assert len(ledger.positions) == 2


def test_rejected_or_canceled_submission_consumes_no_reservation():
    for status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
        ledger = SameCycleEntryReservations.from_snapshot(100.0, [])
        assert not ledger.reserve(_order("AAA", OrderSide.BUY, 8, status), 10.0)
        assert ledger.cash == 100.0
        assert ledger.positions == []


def test_active_short_reserves_position_and_sector_capacity_but_not_long_cash_semantics():
    ledger = SameCycleEntryReservations.from_snapshot(100.0, [])
    assert ledger.reserve(_order("AAA", OrderSide.SELL, 8, OrderStatus.SUBMITTED), 10.0)
    assert ledger.cash == 100.0
    assert len(ledger.positions) == 1
    assert ledger.positions[0].qty == -8


def test_duplicate_symbol_reservation_is_idempotent():
    ledger = SameCycleEntryReservations.from_snapshot(100.0, [])
    order = _order("AAA", OrderSide.BUY, 4, OrderStatus.SUBMITTED)
    assert ledger.reserve(order, 10.0)
    assert not ledger.reserve(order, 10.0)
    assert ledger.cash == 60.0
    assert len(ledger.positions) == 1
