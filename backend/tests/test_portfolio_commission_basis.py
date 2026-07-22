from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.brokers.base import Fill
from app.core.domain import OrderSide
from app.portfolio.tracker import PortfolioTracker

TS = datetime(2026, 7, 15, 14, 30, tzinfo=UTC)


def _fill(order_id: str, side: OrderSide, qty: float, price: float, commission: float, *, minutes: int = 0) -> Fill:
    return Fill(order_id=order_id, symbol="XYZ", side=side, qty=qty, price=price, commission=commission, strategy="fee-test", ts=TS + timedelta(minutes=minutes))


def test_flat_price_long_round_trip_reconciles_all_fees():
    tracker = PortfolioTracker()
    tracker.on_fill(_fill("entry", OrderSide.BUY, 10, 100, 1.25))
    trade = tracker.on_fill(_fill("exit", OrderSide.SELL, 10, 100, 1.75, minutes=5))
    assert trade is not None
    assert trade.pnl == -3.0
    assert tracker.realized_pnl == -3.0


def test_flat_price_short_round_trip_reconciles_all_fees():
    tracker = PortfolioTracker()
    tracker.on_fill(_fill("entry", OrderSide.SELL, 10, 100, 1.25))
    trade = tracker.on_fill(_fill("exit", OrderSide.BUY, 10, 100, 1.75, minutes=5))
    assert trade is not None
    assert trade.pnl == -3.0


def test_partial_closes_allocate_entry_fee_basis_once_pro_rata():
    tracker = PortfolioTracker()
    tracker.on_fill(_fill("entry", OrderSide.BUY, 10, 100, 10.0))
    first = tracker.on_fill(_fill("exit-1", OrderSide.SELL, 4, 100, 4.0, minutes=5))
    second = tracker.on_fill(_fill("exit-2", OrderSide.SELL, 6, 100, 6.0, minutes=10))
    assert first is None
    assert second is not None and second.pnl == -20.0
    assert tracker.realized_pnl == -20.0


def test_adds_accumulate_commission_basis_before_close():
    tracker = PortfolioTracker()
    tracker.on_fill(_fill("entry-1", OrderSide.BUY, 5, 100, 2.0))
    tracker.on_fill(_fill("entry-2", OrderSide.BUY, 5, 100, 3.0, minutes=1))
    trade = tracker.on_fill(_fill("exit", OrderSide.SELL, 10, 100, 4.0, minutes=5))
    assert trade is not None
    assert trade.pnl == -9.0


def test_flip_splits_fill_commission_between_close_and_new_lot():
    tracker = PortfolioTracker()
    tracker.on_fill(_fill("long", OrderSide.BUY, 10, 100, 2.0))
    closed_long = tracker.on_fill(_fill("flip", OrderSide.SELL, 15, 100, 3.0, minutes=5))
    assert closed_long is not None
    assert closed_long.pnl == -4.0
    closed_short = tracker.on_fill(_fill("cover", OrderSide.BUY, 5, 100, 1.0, minutes=10))
    assert closed_short is not None
    assert closed_short.pnl == -2.0
    assert tracker.realized_pnl == -6.0
