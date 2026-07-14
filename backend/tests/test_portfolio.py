"""Portfolio tracker ledger and trade-record tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.brokers.base import Fill
from app.core.domain import OrderSide
from app.portfolio.tracker import PortfolioTracker

T0 = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)


def fill(symbol="AAPL", side=OrderSide.BUY, qty=10.0, price=100.0, ts=T0, reason="") -> Fill:
    return Fill(order_id="o1", symbol=symbol, side=side, qty=qty, price=price, ts=ts,
                strategy="test", reason=reason)


def test_round_trip_long_produces_trade():
    p = PortfolioTracker()
    assert p.on_fill(fill(side=OrderSide.BUY, qty=10, price=100.0)) is None
    trade = p.on_fill(fill(side=OrderSide.SELL, qty=10, price=102.5, ts=T0 + timedelta(minutes=5)))
    assert trade is not None
    assert trade.pnl == pytest.approx(25.0)
    assert trade.side == "long"
    assert trade.pnl_pct == pytest.approx(2.5)
    assert p.realized_pnl == pytest.approx(25.0)
    assert p.day_trades_today == 1  # same-day round trip


def test_short_round_trip():
    p = PortfolioTracker()
    p.on_fill(fill(side=OrderSide.SELL, qty=10, price=100.0))
    trade = p.on_fill(fill(side=OrderSide.BUY, qty=10, price=98.0))
    assert trade.side == "short"
    assert trade.pnl == pytest.approx(20.0)


def test_partial_close_keeps_basis():
    p = PortfolioTracker()
    p.on_fill(fill(qty=100, price=100.0))
    trade = p.on_fill(fill(side=OrderSide.SELL, qty=40, price=101.0))
    assert trade.qty == 40
    assert trade.pnl == pytest.approx(40.0)
    trade2 = p.on_fill(fill(side=OrderSide.SELL, qty=60, price=99.0))
    assert trade2.pnl == pytest.approx(-60.0)
    assert p.realized_pnl == pytest.approx(-20.0)


def test_averaging_in_blends_basis():
    p = PortfolioTracker()
    p.on_fill(fill(qty=10, price=100.0))
    p.on_fill(fill(qty=10, price=110.0))
    trade = p.on_fill(fill(side=OrderSide.SELL, qty=20, price=108.0))
    assert trade.entry_price == pytest.approx(105.0)
    assert trade.pnl == pytest.approx(60.0)


def test_flip_creates_trade_and_new_lot():
    p = PortfolioTracker()
    p.on_fill(fill(qty=10, price=100.0))
    trade = p.on_fill(fill(side=OrderSide.SELL, qty=25, price=101.0))
    assert trade.qty == 10  # only the closed portion
    # Remaining 15 short at 101
    trade2 = p.on_fill(fill(side=OrderSide.BUY, qty=15, price=100.0))
    assert trade2.side == "short"
    assert trade2.pnl == pytest.approx(15.0)


def test_r_multiple_uses_noted_stop():
    p = PortfolioTracker()
    p.on_fill(fill(qty=10, price=100.0))
    p.note_stop("AAPL", 98.0)  # $2 initial risk/share
    trade = p.on_fill(fill(side=OrderSide.SELL, qty=10, price=104.0))
    assert trade.r_multiple == pytest.approx(2.0)


def test_session_roll_resets_day_stats():
    p = PortfolioTracker()
    p.roll_session(T0.date(), 100_000.0)
    p.on_fill(fill(qty=10, price=100.0))
    p.on_fill(fill(side=OrderSide.SELL, qty=10, price=101.0))
    assert p.day_realized_pnl == pytest.approx(10.0)
    changed = p.roll_session(T0.date() + timedelta(days=1), 100_010.0)
    assert changed
    assert p.day_realized_pnl == 0.0
    assert p.day_trades_today == 0
    assert p.day_pnl(100_510.0) == pytest.approx(500.0)


def test_stats_and_equity_curve():
    p = PortfolioTracker()
    p.mark_equity(100_000, T0)
    p.mark_equity(100_500, T0 + timedelta(minutes=1))
    p.mark_equity(100_200, T0 + timedelta(minutes=2))
    assert p.peak_equity == 100_500
    assert p.drawdown_pct(100_200) == pytest.approx(0.2983, abs=1e-3)
    p.on_fill(fill(qty=10, price=100.0))
    p.on_fill(fill(side=OrderSide.SELL, qty=10, price=101.0))
    stats = p.stats()
    assert stats["total_trades"] == 1
    assert stats["win_rate"] == 100.0
