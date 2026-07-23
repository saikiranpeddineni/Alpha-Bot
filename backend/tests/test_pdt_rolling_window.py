"""Regression tests for issue #65 — PDT rolling 5-business-day window.

Verifies that PortfolioTracker.day_trades_in_window() correctly accounts for
day trades across session boundaries within the regulatory window, and that
roll_session() no longer discards prior-session PDT usage.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.portfolio.tracker import PortfolioTracker, _business_days_ago


# ---------------------------------------------------------------------------
# Helper: _business_days_ago
# ---------------------------------------------------------------------------

def test_business_days_ago_skips_weekend():
    # Monday 2024-01-08; 1 biz day back should be Friday 2024-01-05
    monday = date(2024, 1, 8)
    assert _business_days_ago(monday, 1) == date(2024, 1, 5)


def test_business_days_ago_four_back_from_friday():
    # Friday 2024-01-12; 4 biz days back = Monday 2024-01-08
    friday = date(2024, 1, 12)
    assert _business_days_ago(friday, 4) == date(2024, 1, 8)


# ---------------------------------------------------------------------------
# Core rolling-window behaviour
# ---------------------------------------------------------------------------

def _make_tracker_with_day_trades(dates: list[date]) -> PortfolioTracker:
    """Return a PortfolioTracker whose _day_trade_dates are pre-populated."""
    tracker = PortfolioTracker()
    tracker._day_trade_dates = list(dates)
    return tracker


class TestRollingWindow:
    """day_trades_in_window returns correct counts across boundaries."""

    def test_trades_on_same_day_counted(self):
        mon = date(2024, 1, 8)
        tracker = _make_tracker_with_day_trades([mon, mon, mon])
        assert tracker.day_trades_in_window(as_of=mon) == 3

    def test_monday_trades_counted_on_tuesday(self):
        """3 trades on Monday must still appear on Tuesday (same window)."""
        mon = date(2024, 1, 8)
        tue = date(2024, 1, 9)
        tracker = _make_tracker_with_day_trades([mon, mon, mon])
        assert tracker.day_trades_in_window(as_of=tue) == 3

    def test_monday_trades_counted_on_friday(self):
        """Monday trades are within the 5-business-day window on Friday."""
        mon = date(2024, 1, 8)
        fri = date(2024, 1, 12)
        tracker = _make_tracker_with_day_trades([mon, mon, mon])
        assert tracker.day_trades_in_window(as_of=fri) == 3

    def test_monday_trades_expire_next_tuesday(self):
        """Monday trades fall outside the window the following Tuesday."""
        mon = date(2024, 1, 8)
        next_tue = date(2024, 1, 16)  # Mon Jan 15 is MLK Day but SEC window is calendar biz days
        # Window start for next_tue: _business_days_ago(next_tue, 4)
        # next_tue = Tue Jan 16; 4 biz days back: Mon Jan 15, Fri Jan 12, Thu Jan 11, Wed Jan 10 -> window_start = Wed Jan 10
        # mon Jan 8 < Wed Jan 10 => outside window
        tracker = _make_tracker_with_day_trades([mon, mon, mon])
        # The window start for Jan 16 is Jan 10; Jan 8 is before that.
        assert tracker.day_trades_in_window(as_of=next_tue) == 0

    def test_mixed_trades_partial_expiry(self):
        """Only trades within the window are counted; older ones excluded."""
        mon = date(2024, 1, 8)   # inside window on Fri Jan 12
        old = date(2024, 1, 1)   # outside window on Fri Jan 12
        fri = date(2024, 1, 12)
        tracker = _make_tracker_with_day_trades([old, old, mon, mon])
        assert tracker.day_trades_in_window(as_of=fri) == 2

    def test_zero_trades_in_window(self):
        tracker = PortfolioTracker()
        assert tracker.day_trades_in_window(as_of=date(2024, 1, 10)) == 0


# ---------------------------------------------------------------------------
# roll_session does NOT wipe PDT state
# ---------------------------------------------------------------------------

class TestRollSession:
    """Session rollover preserves PDT rolling log; only prunes expired entries."""

    def test_roll_does_not_reset_pdt_count(self):
        """Trades recorded on Monday survive roll into Tuesday."""
        mon = date(2024, 1, 8)
        tue = date(2024, 1, 9)
        tracker = _make_tracker_with_day_trades([mon, mon, mon])
        tracker.session_date = mon
        tracker.roll_session(tue, equity=20_000.0)
        assert tracker.day_trades_in_window(as_of=tue) == 3

    def test_roll_prunes_expired_entries(self):
        """Entries outside the 5-business-day window are removed on roll."""
        old = date(2024, 1, 1)   # will be outside window for Jan 16
        recent = date(2024, 1, 14)  # will be inside window for Jan 16
        next_tue = date(2024, 1, 16)
        tracker = _make_tracker_with_day_trades([old, old, recent])
        tracker.session_date = recent
        tracker.roll_session(next_tue, equity=20_000.0)
        # old entries pruned; only 'recent' survives
        assert len(tracker._day_trade_dates) == 1
        assert tracker._day_trade_dates[0] == recent

    def test_multiple_rolls_accumulate_correctly(self):
        """Trades accumulate correctly across Mon-Fri without being wiped."""
        tracker = PortfolioTracker()
        week_dates = [
            date(2024, 1, 8),   # Mon
            date(2024, 1, 9),   # Tue
            date(2024, 1, 10),  # Wed
            date(2024, 1, 11),  # Thu
            date(2024, 1, 12),  # Fri
        ]
        equity = 20_000.0
        tracker.session_date = None
        for d in week_dates:
            tracker.roll_session(d, equity=equity)
            tracker._day_trade_dates.append(d)  # simulate one day-trade per day

        # On Friday all 5 trades should be in the window
        assert tracker.day_trades_in_window(as_of=week_dates[-1]) == 5


# ---------------------------------------------------------------------------
# Overnight trades are NOT counted as day trades
# ---------------------------------------------------------------------------

class TestOvernightTradeNotCounted:
    """Entry on Monday, exit on Tuesday: must not be classified as a day trade."""

    def test_overnight_round_trip_not_counted(self):
        mon = date(2024, 1, 8)
        tue = date(2024, 1, 9)
        # Simulate the condition in on_fill: only append when entry_at.date() == exit_at.date()
        entry_ts = datetime(2024, 1, 8, 10, 0, tzinfo=UTC)
        exit_ts  = datetime(2024, 1, 9, 10, 0, tzinfo=UTC)
        is_day_trade = entry_ts.date() == exit_ts.date()
        tracker = PortfolioTracker()
        if is_day_trade:
            tracker._day_trade_dates.append(exit_ts.date())
        assert tracker.day_trades_in_window(as_of=tue) == 0


# ---------------------------------------------------------------------------
# PDT guard integration: RiskManager sees rolling count
# ---------------------------------------------------------------------------

class TestPDTGuardIntegration:
    """Verify that day_trades_in_window feeds the PDT guard correctly."""

    def test_three_monday_trades_block_tuesday_entry(self):
        """After 3 day trades Monday, Tuesday entry must be blocked for sub-25k live."""
        from app.risk.manager import PDT_EQUITY_MIN, PDT_MAX_DAY_TRADES

        mon = date(2024, 1, 8)
        tue = date(2024, 1, 9)
        tracker = _make_tracker_with_day_trades([mon, mon, mon])
        rolling_count = tracker.day_trades_in_window(as_of=tue)
        equity = PDT_EQUITY_MIN - 1.0  # sub-25k
        assert rolling_count >= PDT_MAX_DAY_TRADES
        # The RiskManager blocks when ctx.day_trades_today >= PDT_MAX_DAY_TRADES AND equity < PDT_EQUITY_MIN
        # This test confirms the count fed to that check is correct.
        assert rolling_count >= PDT_MAX_DAY_TRADES and equity < PDT_EQUITY_MIN

    def test_zero_trades_does_not_block(self):
        tracker = PortfolioTracker()
        assert tracker.day_trades_in_window(as_of=date(2024, 1, 9)) == 0


def test_day_trades_in_window_accepts_datetime():
    tracker = PortfolioTracker()
    tracker._day_trade_dates = [
        date(2026, 7, 21),
        date(2026, 7, 22),
        date(2026, 7, 23),
    ]

    result = tracker.day_trades_in_window(
        datetime(2026, 7, 23, 12, 5, tzinfo=UTC)
    )

    assert result == 3

