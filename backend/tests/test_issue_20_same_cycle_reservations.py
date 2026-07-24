from app.core.domain import Order, OrderSide, OrderStatus, OrderType, Position
from app.core.entry_reservations import SameCycleEntryReservations


def _order(symbol: str, side: OrderSide, qty: float, status: OrderStatus) -> Order:
    return Order(
        id=f"id-{symbol}",
        client_order_id=f"client-{symbol}",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        status=status,
    )


def test_active_long_submission_reserves_cash_and_position_exposure() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])

    assert reservations.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.PENDING), 10.0)
    assert reservations.cash == 20.0
    assert len(reservations.positions) == 1
    assert reservations.positions[0].symbol == "AAA"
    assert reservations.positions[0].qty == 8


def test_two_longs_cannot_reuse_original_cash_snapshot() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])

    reservations.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.PENDING), 10.0)
    reservations.reserve(_order("BBB", OrderSide.BUY, 8, OrderStatus.PENDING), 10.0)

    # Reservation ledger cannot manufacture reusable long cash; the second
    # reservation consumes only the remaining basis and floors at zero.
    assert reservations.cash == 0.0
    assert {p.symbol for p in reservations.positions} == {"AAA", "BBB"}


def test_rejected_submission_does_not_consume_reservation() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])

    assert not reservations.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.REJECTED), 10.0)
    assert reservations.cash == 100.0
    assert reservations.positions == []


def test_canceled_submission_does_not_consume_reservation() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])

    assert not reservations.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.CANCELED), 10.0)
    assert reservations.cash == 100.0
    assert reservations.positions == []


def test_ambiguous_pending_submission_remains_reserved() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])

    assert reservations.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.PENDING), 10.0)
    assert reservations.cash == 20.0
    assert reservations.positions[0].symbol == "AAA"


def test_partially_filled_submission_keeps_full_conservative_reservation() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])

    assert reservations.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.PARTIALLY_FILLED), 10.0)
    assert reservations.cash == 20.0
    assert reservations.positions[0].qty == 8


def test_immediately_filled_submission_remains_reserved_for_stale_cycle_snapshot() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])

    assert reservations.reserve(_order("AAA", OrderSide.BUY, 8, OrderStatus.FILLED), 10.0)
    assert reservations.cash == 20.0
    assert reservations.positions[0].qty == 8


def test_short_reserves_exposure_but_does_not_apply_long_cash_semantics() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])

    assert reservations.reserve(_order("SHORT", OrderSide.SELL, 5, OrderStatus.PENDING), 10.0)
    assert reservations.cash == 100.0
    assert reservations.positions[0].qty == -5


def test_existing_positions_are_preserved_and_new_reservation_is_added() -> None:
    existing = Position(
        symbol="OLD",
        qty=2,
        avg_entry_price=20.0,
        market_price=21.0,
        strategy="existing",
    )
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [existing])

    reservations.reserve(_order("NEW", OrderSide.BUY, 2, OrderStatus.PENDING), 10.0)

    assert [p.symbol for p in reservations.positions] == ["OLD", "NEW"]
    assert reservations.cash == 80.0


def test_duplicate_symbol_is_not_double_reserved() -> None:
    reservations = SameCycleEntryReservations.from_snapshot(100.0, [])
    order = _order("AAA", OrderSide.BUY, 5, OrderStatus.PENDING)

    assert reservations.reserve(order, 10.0)
    assert not reservations.reserve(order, 10.0)
    assert reservations.cash == 50.0
    assert len(reservations.positions) == 1
