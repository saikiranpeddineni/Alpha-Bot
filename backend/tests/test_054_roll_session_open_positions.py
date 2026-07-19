"""Regression tests for issue #118 / 054.

Verifies that PortfolioTracker.roll_session() correctly handles open positions
at session boundaries:
- No open positions: rolls silently, counters reset, returns truthy RollResult.
- Open positions: rolls, logs WARNING, returns truthy RollResult with open_symbols.
- Same session: no-op, returns falsy RollResult.
- RollResult is backwards-compatible with bool callers.
- day_realized_pnl and day_trades_today are reset on every roll.
- session_start_equity captures overnight equity baseline.
- Long and short carry-forward positions are both detected.
- Multiple open positions all appear in open_symbols.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest

from app.brokers.base import Fill
from app.core.domain import OrderSide
from app.portfolio.tracker import PortfolioTracker, RollResult

DAY0 = date(2026, 1, 6)
DAY1 = date(2026, 1, 7)
DAY2 = date(2026, 1, 8)


def _ts(d: date, hour: int = 10) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=UTC)


def _fill(
    symbol: str = "AAPL",
    side: OrderSide = OrderSide.BUY,
    qty: float = 100.0,
    price: float = 150.0,
    ts: datetime | None = None,
) -> Fill:
    return Fill(
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        strategy="test",
        reason="entry",
        order_id="o1",
        ts=ts or _ts(DAY0),
    )


# ---------------------------------------------------------------------------
# RollResult semantics
# ---------------------------------------------------------------------------

def test_roll_result_truthy_when_rolled():
    r = RollResult(rolled=True, open_symbols=("AAPL",))
    assert bool(r) is True


def test_roll_result_falsy_when_not_rolled():
    r = RollResult(rolled=False)
    assert bool(r) is False


def test_roll_result_open_symbols_empty_by_default():
    r = RollResult(rolled=True)
    assert r.open_symbols == ()


# ---------------------------------------------------------------------------
# No open positions: clean roll
# ---------------------------------------------------------------------------

def test_roll_no_open_positions_returns_true():
    p = PortfolioTracker()
    result = p.roll_session(DAY0, 100_000.0)
    assert result.rolled is True
    assert result.open_symbols == ()


def test_roll_no_open_positions_resets_counters():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    # Simulate intraday P&L and a day-trade
    p.day_realized_pnl = 500.0
    p.day_trades_today = 2
    result = p.roll_session(DAY1, 100_500.0)
    assert result.rolled is True
    assert p.day_realized_pnl == pytest.approx(0.0)
    assert p.day_trades_today == 0
    assert p.session_start_equity == pytest.approx(100_500.0)


def test_same_session_returns_false():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    result = p.roll_session(DAY0, 100_000.0)  # same date
    assert result.rolled is False
    assert bool(result) is False


# ---------------------------------------------------------------------------
# Open LONG position at session boundary
# ---------------------------------------------------------------------------

def test_roll_with_long_position_returns_symbol(caplog):
    import logging
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    p.on_fill(_fill("AAPL", OrderSide.BUY, qty=100, price=150.0))

    with caplog.at_level(logging.WARNING, logger="root"):
        result = p.roll_session(DAY1, 99_000.0)

    assert result.rolled is True
    assert "AAPL" in result.open_symbols


def test_roll_with_long_position_logs_warning(caplog):
    import logging
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    p.on_fill(_fill("AAPL", OrderSide.BUY, qty=100, price=150.0))

    with caplog.at_level(logging.WARNING):
        p.roll_session(DAY1, 99_000.0)

    # At least one warning message must mention AAPL and "carrying"
    warned = any(
        "AAPL" in r.message and "carrying" in r.message.lower()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )
    assert warned, f"Expected carry-forward warning for AAPL, got: {[r.message for r in caplog.records]}"


def test_roll_with_long_position_resets_counters():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    p.on_fill(_fill("AAPL", OrderSide.BUY, qty=100, price=150.0))
    p.day_realized_pnl = -800.0
    p.day_trades_today = 3

    p.roll_session(DAY1, 99_000.0)

    assert p.day_realized_pnl == pytest.approx(0.0)
    assert p.day_trades_today == 0
    assert p.session_start_equity == pytest.approx(99_000.0)


# ---------------------------------------------------------------------------
# Open SHORT position at session boundary
# ---------------------------------------------------------------------------

def test_roll_with_short_position_returns_symbol():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    p.on_fill(_fill("TSLA", OrderSide.SELL, qty=50, price=200.0))

    result = p.roll_session(DAY1, 101_000.0)
    assert result.rolled is True
    assert "TSLA" in result.open_symbols


# ---------------------------------------------------------------------------
# Multiple open positions
# ---------------------------------------------------------------------------

def test_roll_with_multiple_open_positions():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    p.on_fill(_fill("AAPL", OrderSide.BUY, qty=100, price=150.0))
    p.on_fill(_fill("MSFT", OrderSide.BUY, qty=50, price=300.0))
    p.on_fill(_fill("TSLA", OrderSide.SELL, qty=20, price=250.0))

    result = p.roll_session(DAY1, 98_000.0)
    assert result.rolled is True
    assert set(result.open_symbols) == {"AAPL", "MSFT", "TSLA"}


# ---------------------------------------------------------------------------
# Position fully closed before roll: not reported
# ---------------------------------------------------------------------------

def test_closed_position_not_reported_in_roll():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    p.on_fill(_fill("AAPL", OrderSide.BUY, qty=100, price=150.0))
    # Close the position fully on the same day
    p.on_fill(_fill("AAPL", OrderSide.SELL, qty=100, price=155.0))

    result = p.roll_session(DAY1, 100_500.0)
    assert result.rolled is True
    assert "AAPL" not in result.open_symbols
    assert result.open_symbols == ()


# ---------------------------------------------------------------------------
# Backwards-compatibility: if-statement callers still work
# ---------------------------------------------------------------------------

def test_if_roll_session_works_when_rolled():
    p = PortfolioTracker()
    rolled = False
    if p.roll_session(DAY0, 100_000.0):
        rolled = True
    assert rolled


def test_if_roll_session_works_when_not_rolled():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    rolled = False
    if p.roll_session(DAY0, 100_000.0):
        rolled = True  # pragma: no cover
    assert not rolled


# ---------------------------------------------------------------------------
# open_positions() helper
# ---------------------------------------------------------------------------

def test_open_positions_empty_initially():
    p = PortfolioTracker()
    assert p.open_positions() == {}


def test_open_positions_after_fill():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    p.on_fill(_fill("AAPL", OrderSide.BUY, qty=100, price=150.0))
    ops = p.open_positions()
    assert "AAPL" in ops
    assert ops["AAPL"].qty == pytest.approx(100.0)


def test_open_positions_cleared_after_close():
    p = PortfolioTracker()
    p.roll_session(DAY0, 100_000.0)
    p.on_fill(_fill("AAPL", OrderSide.BUY, qty=100, price=150.0))
    p.on_fill(_fill("AAPL", OrderSide.SELL, qty=100, price=155.0))
    assert p.open_positions() == {}
