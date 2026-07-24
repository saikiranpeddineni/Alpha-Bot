from datetime import UTC, date, datetime

from app.brokers.base import Fill
from app.core.domain import OrderSide
from app.portfolio.tracker import PortfolioTracker


def test_checkpoint_round_trip_preserves_rolling_day_trade_history() -> None:
    tracker = PortfolioTracker()
    tracker.session_date = date(2026, 7, 24)
    tracker._day_trade_dates = [
        date(2026, 7, 20),
        date(2026, 7, 22),
        date(2026, 7, 24),
    ]

    restored = PortfolioTracker()
    restored.restore_checkpoint(tracker.checkpoint(account_id="acct-1"))

    assert restored._day_trade_dates == tracker._day_trade_dates
    assert restored.day_trades_in_window(date(2026, 7, 24)) == 3


def test_same_session_restart_does_not_restore_pdt_capacity() -> None:
    tracker = PortfolioTracker()
    tracker.session_date = date(2026, 7, 24)
    tracker._day_trade_dates = [date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)]

    restored = PortfolioTracker()
    restored.restore_checkpoint(tracker.checkpoint())
    restored.roll_session(date(2026, 7, 24), 10000.0)

    assert restored.day_trades_in_window(date(2026, 7, 24)) == 3


def test_weekend_does_not_expire_business_day_history() -> None:
    tracker = PortfolioTracker()
    # Friday, Monday and Tuesday remain in Wednesday's five-business-day window.
    tracker._day_trade_dates = [date(2026, 7, 17), date(2026, 7, 20), date(2026, 7, 21)]

    assert tracker.day_trades_in_window(date(2026, 7, 22)) == 3


def test_old_day_trades_expire_outside_five_business_day_window() -> None:
    tracker = PortfolioTracker()
    tracker._day_trade_dates = [
        date(2026, 7, 13),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
    ]

    assert tracker.day_trades_in_window(date(2026, 7, 22)) == 3


def test_multiple_round_trips_on_same_day_are_preserved() -> None:
    tracker = PortfolioTracker()
    tracker.session_date = date(2026, 7, 24)
    tracker._day_trade_dates = [date(2026, 7, 24), date(2026, 7, 24), date(2026, 7, 24)]

    restored = PortfolioTracker()
    restored.restore_checkpoint(tracker.checkpoint())

    assert restored.day_trades_today == 3
    assert restored.day_trades_in_window(date(2026, 7, 24)) == 3


def test_legacy_checkpoint_restores_same_day_counter_conservatively() -> None:
    restored = PortfolioTracker()
    restored.restore_checkpoint({
        "session_date": "2026-07-24",
        "session_start_equity": 10000.0,
        "peak_equity": 10000.0,
        "day_trades_today": 2,
        "trades": [],
        "equity_curve": [],
        "lots": {},
    })

    assert restored.day_trades_today == 2
    assert restored.day_trades_in_window(date(2026, 7, 24)) == 2


def test_checkpoint_keeps_account_identity_with_pdt_history() -> None:
    tracker = PortfolioTracker()
    tracker.session_date = date(2026, 7, 24)
    tracker._day_trade_dates = [date(2026, 7, 24)]

    checkpoint = tracker.checkpoint(account_id="live-account-A")

    assert checkpoint["account_id"] == "live-account-A"
    assert checkpoint["day_trade_dates"] == ["2026-07-24"]


def test_partial_exits_count_one_day_trade_only_when_position_cycle_closes() -> None:
    tracker = PortfolioTracker()
    ts = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)
    tracker.session_date = ts.date()

    tracker.on_fill(Fill("entry", "AAA", OrderSide.BUY, 100, 10.0, ts=ts))
    tracker.on_fill(Fill("exit-1", "AAA", OrderSide.SELL, 25, 10.5, ts=ts))
    tracker.on_fill(Fill("exit-2", "AAA", OrderSide.SELL, 25, 10.6, ts=ts))

    assert tracker.day_trades_today == 0

    tracker.on_fill(Fill("exit-3", "AAA", OrderSide.SELL, 50, 10.7, ts=ts))

    assert tracker.day_trades_today == 1


def test_partial_exit_day_trade_count_survives_restart() -> None:
    tracker = PortfolioTracker()
    ts = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)
    tracker.session_date = ts.date()

    tracker.on_fill(Fill("entry", "AAA", OrderSide.BUY, 100, 10.0, ts=ts))
    tracker.on_fill(Fill("exit-1", "AAA", OrderSide.SELL, 40, 10.5, ts=ts))
    tracker.on_fill(Fill("exit-2", "AAA", OrderSide.SELL, 60, 10.6, ts=ts))

    restored = PortfolioTracker()
    restored.restore_checkpoint(tracker.checkpoint(account_id="acct-1"))

    assert tracker.day_trades_today == 1
    assert restored.day_trades_today == 1
    assert restored.day_trades_in_window(ts.date()) == 1
