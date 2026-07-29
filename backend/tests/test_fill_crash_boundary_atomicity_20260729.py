"""Crash-boundary contracts for exactly-once broker fill accounting.

These tests exercise the durable boundary rather than broker transport:
portfolio checkpoint + cumulative fill watermark + closed trade must commit as one
transaction, and a process restart must never economically reapply an acknowledged fill.
"""
from datetime import UTC, datetime

import pytest

from app.brokers.base import Fill
from app.core.domain import OrderSide
from app.db.database import Database
from app.portfolio.tracker import PortfolioTracker


def _fill(order_id: str, side: OrderSide, cumulative: float, price: float, *, symbol="AAPL") -> Fill:
    return Fill(
        order_id=order_id,
        symbol=symbol,
        side=side,
        qty=cumulative,
        price=price,
        strategy="crash-boundary",
        ts=datetime.now(UTC),
        broker_fill_key=order_id,
        cumulative_qty=cumulative,
        broker_mode="paper",
        broker_account_id="acct-crash",
    )


def test_restart_after_durable_commit_before_broker_ack_does_not_reapply_fill(tmp_path):
    db = Database(tmp_path / "bot.db")
    tracker = PortfolioTracker(restore_mode="paper", restore_account_id="acct-crash")
    fill = _fill("entry-atomic", OrderSide.BUY, 7, 101.0)

    # on_fill is the durable commit boundary. Simulate process death immediately
    # afterward, before AlpacaBroker.ack_fill_processed can advance volatile state.
    tracker.on_fill(fill)
    assert db.processed_fill_qty("paper", "acct-crash", "entry-atomic") == 7

    restarted = PortfolioTracker(restore_mode="paper", restore_account_id="acct-crash")
    assert restarted._lots["AAPL"].qty == 7
    assert restarted.on_fill(fill) is None
    assert restarted._lots["AAPL"].qty == 7


def test_closed_trade_and_exit_watermark_survive_same_crash_boundary(tmp_path):
    db = Database(tmp_path / "bot.db")
    tracker = PortfolioTracker(restore_mode="paper", restore_account_id="acct-crash")
    tracker.on_fill(_fill("entry", OrderSide.BUY, 5, 100.0))
    exit_fill = _fill("exit", OrderSide.SELL, 5, 110.0)
    trade = tracker.on_fill(exit_fill)
    assert trade is not None and trade.pnl == pytest.approx(50.0)

    restarted = PortfolioTracker(restore_mode="paper", restore_account_id="acct-crash")
    assert "AAPL" not in restarted._lots
    assert restarted.realized_pnl == pytest.approx(50.0)
    assert len(restarted.trades) == 1
    assert restarted.on_fill(exit_fill) is None
    assert restarted.realized_pnl == pytest.approx(50.0)
    assert len(restarted.trades) == 1
    assert db.processed_fill_qty("paper", "acct-crash", "exit") == 5


def test_failed_atomic_commit_restores_memory_and_leaves_no_watermark(tmp_path, monkeypatch):
    db = Database(tmp_path / "bot.db")
    tracker = PortfolioTracker(restore_mode="paper", restore_account_id="acct-crash")
    fill = _fill("entry-fail", OrderSide.BUY, 4, 99.0)

    original = db.commit_fill_processing

    def fail_before_commit(**kwargs):
        raise RuntimeError("crash before durable transaction")

    monkeypatch.setattr(db, "commit_fill_processing", fail_before_commit)
    with pytest.raises(RuntimeError, match="crash before durable transaction"):
        tracker.on_fill(fill)
    assert "AAPL" not in tracker._lots
    assert db.processed_fill_qty("paper", "acct-crash", "entry-fail") == 0

    monkeypatch.setattr(db, "commit_fill_processing", original)
    tracker.on_fill(fill)
    assert tracker._lots["AAPL"].qty == 4
    assert db.processed_fill_qty("paper", "acct-crash", "entry-fail") == 4
