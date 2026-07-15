from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.brokers.base import Fill
from app.core.domain import OrderSide
from app.db.database import Database
from app.portfolio.tracker import PortfolioTracker


def broker_fill(order_id: str, side: OrderSide, cumulative: float, price: float, *, symbol: str = "AAPL", ts=None) -> Fill:
    return Fill(
        order_id=order_id,
        symbol=symbol,
        side=side,
        qty=cumulative,
        price=price,
        strategy="restart-test",
        ts=ts or datetime.now(UTC),
        broker_fill_key=order_id,
        cumulative_qty=cumulative,
        broker_mode="paper",
    )


def test_crash_restart_replays_unprocessed_fill_exactly_once(tmp_path):
    db = Database(tmp_path / "bot.db")
    first = PortfolioTracker()
    fill = broker_fill("entry-1", OrderSide.BUY, 10, 100)

    # Broker execution exists but the prior process dies before local application.
    restarted = PortfolioTracker()
    restarted.on_fill(fill)
    assert restarted._lots["AAPL"].qty == 10
    assert db.processed_fill_qty("entry-1") == 10

    # The broker may surface the same cumulative watermark forever; it is idempotent.
    restarted.on_fill(fill)
    assert restarted._lots["AAPL"].qty == 10

    second_restart = PortfolioTracker()
    assert second_restart._lots["AAPL"].qty == 10
    second_restart.on_fill(fill)
    assert second_restart._lots["AAPL"].qty == 10


def test_partial_fill_watermarks_apply_only_monotonic_delta_in_order(tmp_path):
    Database(tmp_path / "bot.db")
    tracker = PortfolioTracker()
    t0 = datetime(2026, 7, 15, 14, 30, tzinfo=UTC)

    tracker.on_fill(broker_fill("partial", OrderSide.BUY, 3, 100, ts=t0))
    tracker.on_fill(broker_fill("partial", OrderSide.BUY, 7, 102, ts=t0 + timedelta(seconds=1)))
    tracker.on_fill(broker_fill("partial", OrderSide.BUY, 10, 101, ts=t0 + timedelta(seconds=2)))

    assert tracker._lots["AAPL"].qty == 10
    assert tracker._lots["AAPL"].avg_price == pytest.approx((3 * 100 + 4 * 102 + 3 * 101) / 10)

    restarted = PortfolioTracker()
    restarted.on_fill(broker_fill("partial", OrderSide.BUY, 10, 101, ts=t0 + timedelta(seconds=2)))
    assert restarted._lots["AAPL"].qty == 10


def test_long_exit_and_short_cover_restore_symmetrically(tmp_path):
    Database(tmp_path / "bot.db")
    tracker = PortfolioTracker()
    t0 = datetime(2026, 7, 15, 14, 30, tzinfo=UTC)

    tracker.on_fill(broker_fill("long-entry", OrderSide.BUY, 5, 100, symbol="AAPL", ts=t0))
    long_trade = tracker.on_fill(broker_fill("long-exit", OrderSide.SELL, 5, 110, symbol="AAPL", ts=t0 + timedelta(minutes=1)))
    tracker.on_fill(broker_fill("short-entry", OrderSide.SELL, 4, 50, symbol="MSFT", ts=t0 + timedelta(minutes=2)))
    short_trade = tracker.on_fill(broker_fill("short-cover", OrderSide.BUY, 4, 45, symbol="MSFT", ts=t0 + timedelta(minutes=3)))

    assert long_trade is not None and long_trade.pnl == 50
    assert short_trade is not None and short_trade.pnl == 20
    assert tracker._lots == {}

    restarted = PortfolioTracker()
    assert restarted._lots == {}
    assert restarted.realized_pnl == 70
    assert len(restarted.trades) == 2

    # Replayed broker history cannot duplicate either trade.
    assert restarted.on_fill(broker_fill("long-exit", OrderSide.SELL, 5, 110, symbol="AAPL", ts=t0 + timedelta(minutes=1))) is None
    assert restarted.on_fill(broker_fill("short-cover", OrderSide.BUY, 4, 45, symbol="MSFT", ts=t0 + timedelta(minutes=3))) is None
    assert len(restarted.trades) == 2


def test_database_failure_rolls_back_in_memory_fill_application(tmp_path, monkeypatch):
    db = Database(tmp_path / "bot.db")
    tracker = PortfolioTracker()
    fill = broker_fill("atomic", OrderSide.BUY, 8, 100)

    def fail_commit(**kwargs):
        raise RuntimeError("simulated durable commit failure")

    monkeypatch.setattr(db, "commit_fill_processing", fail_commit)
    with pytest.raises(RuntimeError, match="durable commit failure"):
        tracker.on_fill(fill)

    assert "AAPL" not in tracker._lots
    assert db.processed_fill_qty("atomic") == 0


def test_checkpoint_and_fill_watermark_commit_together(tmp_path):
    db = Database(tmp_path / "bot.db")
    tracker = PortfolioTracker()
    tracker.on_fill(broker_fill("entry", OrderSide.BUY, 6, 25))

    checkpoint = db.get_portfolio_checkpoint("paper")
    assert db.processed_fill_qty("entry") == 6
    assert checkpoint is not None
    assert checkpoint["lots"]["AAPL"]["qty"] == 6
