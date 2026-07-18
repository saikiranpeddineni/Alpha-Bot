from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.brokers.base import Fill
from app.core.domain import OrderSide
from app.portfolio.tracker import PortfolioTracker


def _fill(order_id: str, side: OrderSide, qty: float, price: float, *, symbol: str = "XYZ") -> Fill:
    return Fill(
        order_id=order_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        strategy="risk-binding-test",
        ts=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
    )


def test_closing_fill_cannot_override_entry_bound_initial_risk():
    tracker = PortfolioTracker()
    tracker.on_fill(_fill("entry", OrderSide.BUY, 10, 100), initial_stop=95)

    exit_fill = _fill("exit", OrderSide.SELL, 10, 110)
    exit_fill.ts += timedelta(minutes=5)
    trade = tracker.on_fill(exit_fill, initial_stop=99)

    assert trade is not None
    assert trade.r_multiple == 2.0


def test_fill_without_entry_metadata_does_not_borrow_unrelated_stop():
    tracker = PortfolioTracker()
    tracker.on_fill(_fill("manual-entry", OrderSide.BUY, 10, 100))

    exit_fill = _fill("manual-exit", OrderSide.SELL, 10, 110)
    exit_fill.ts += timedelta(minutes=5)
    trade = tracker.on_fill(exit_fill, initial_stop=95)

    assert trade is not None
    assert trade.r_multiple is None


def test_long_and_short_entry_risk_are_bound_symmetrically():
    long_tracker = PortfolioTracker()
    long_tracker.on_fill(_fill("long-entry", OrderSide.BUY, 10, 100), initial_stop=95)
    long_exit = _fill("long-exit", OrderSide.SELL, 10, 110)
    long_exit.ts += timedelta(minutes=5)
    long_trade = long_tracker.on_fill(long_exit)

    short_tracker = PortfolioTracker()
    short_tracker.on_fill(_fill("short-entry", OrderSide.SELL, 10, 100), initial_stop=105)
    short_exit = _fill("short-exit", OrderSide.BUY, 10, 90)
    short_exit.ts += timedelta(minutes=5)
    short_trade = short_tracker.on_fill(short_exit)

    assert long_trade is not None and long_trade.r_multiple == 2.0
    assert short_trade is not None and short_trade.r_multiple == 2.0
