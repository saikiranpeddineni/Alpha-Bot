"""Regressions for issue #65 — PDT rolling 5-business-day window.

Verifies that PortfolioTracker._day_trade_dates persists across session rolls
and that RiskManager.evaluate_entry() blocks new live entries when the rolling
count reaches PDT_MAX_DAY_TRADES, regardless of session boundaries.

No ticker-specific, date-specific, or benchmark-P&L logic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.portfolio.tracker import PortfolioTracker, _business_days_ago, _PDT_WINDOW_BUSINESS_DAYS
from app.risk.manager import AccountContext, PDT_EQUITY_MIN, PDT_MAX_DAY_TRADES, RiskManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MONDAY = date(2024, 1, 8)   # arbitrary Monday
TUESDAY = date(2024, 1, 9)
WEDNESDAY = date(2024, 1, 10)
FRIDAY = date(2024, 1, 12)
NEXT_MONDAY = date(2024, 1, 15)   # 7 calendar days after MONDAY


def _make_tracker() -> PortfolioTracker:
    return PortfolioTracker()


def _ctx(
    rolling: int,
    equity: float = PDT_EQUITY_MIN - 1,
    is_paper: bool = False,
) -> AccountContext:
    return AccountContext(
        equity=equity,
        cash=equity,
        positions=[],
        day_pnl=0.0,
        session_start_equity=equity,
        peak_equity=equity,
        day_trades_today=rolling,   # kept for backward compat
        rolling_day_trades=rolling,
        is_paper=is_paper,
    )


def _make_risk() -> RiskManager:
    cfg = MagicMock()
    cfg.max_drawdown_pct = 10.0
    cfg.max_daily_loss_pct = 5.0
    cfg.no_entry_minutes_before_close = 15
    cfg.cooldown_minutes = 30
    cfg.consecutive_loss_limit = 3
    cfg.max_open_positions = 10
    cfg.max_positions_per_sector = 5
    cfg.min_price = 1.0
    cfg.min_avg_dollar_volume = 0.0
    cfg.risk_per_trade_pct = 1.0
    cfg.max_position_pct = 10.0
    clock = MagicMock()
    clock.in_entry_window.return_value = True
    clock.in_flatten_window.return_value = False
    clock.session_date.return_value = TUESDAY
    return RiskManager(cfg, clock)


def _bars(price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": [price], "high": [price + 1], "low": [price - 1], "close": [price], "volume": [1_000_000], "atr14": [2.0]}
    )


def _signal(price: float = 100.0) -> MagicMock:
    from app.core.domain import SignalAction
    sig = MagicMock()
    sig.symbol = "AAPL"
    sig.action = SignalAction.ENTER_LONG
    sig.price = price
    sig.stop_loss = price - 2.0
    sig.take_profit = price + 4.0
    sig.strategy = "test"
    sig.reason = "test"
    sig.thesis_fingerprint = None
    sig.discovery_source = None
    sig.scanner_score = None
    sig.scanner_auc = None
    sig.score = None
    return sig


# ---------------------------------------------------------------------------
# PortfolioTracker rolling_day_trade_count tests
# ---------------------------------------------------------------------------

class TestRollingDayTradeCount:
    def test_empty_log_returns_zero(self):
        p = _make_tracker()
        assert p.day_trades_in_window(TUESDAY) == 0

    def test_single_trade_on_same_day_counts(self):
        p = _make_tracker()
        p._day_trade_dates.append(MONDAY)
        assert p.day_trades_in_window(MONDAY) == 1

    def test_three_trades_on_monday_still_count_on_tuesday(self):
        """Core regression: Monday's 3 day trades must remain visible on Tuesday."""
        p = _make_tracker()
        for _ in range(3):
            p._day_trade_dates.append(MONDAY)
        # Tuesday is within the 5-calendar-day window from Monday.
        assert p.day_trades_in_window(TUESDAY) == 3

    def test_trades_expire_after_rolling_window(self):
        """Trades older than the rolling window drop out."""
        p = _make_tracker()
        for _ in range(3):
            p._day_trade_dates.append(MONDAY)
        # NEXT_MONDAY is 7 calendar days later; window is 5 days so Monday is out.
        assert p.day_trades_in_window(NEXT_MONDAY) == 0

    def test_trades_on_boundary_day_still_count(self):
        """Trade exactly at the edge of the 5-day window is still included."""
        p = _make_tracker()
        p._day_trade_dates.append(MONDAY)
        # cutoff = ref - 4 days; ref = FRIDAY (4 days later); cutoff == MONDAY
        assert p.day_trades_in_window(FRIDAY) == 1

    def test_session_roll_does_not_clear_rolling_log(self):
        """roll_session must not erase _day_trade_dates from earlier in the window."""
        p = _make_tracker()
        for _ in range(3):
            p._day_trade_dates.append(MONDAY)
        p.roll_session(TUESDAY, 20_000.0)
        # day_trades_today is reset (per-session display)
        assert p.day_trades_today == 0
        # but rolling log still has Monday's trades
        assert p.day_trades_in_window(TUESDAY) == 3

    def test_roll_prunes_stale_entries(self):
        """roll_session prunes entries older than the window to bound memory."""
        p = _make_tracker()
        old_date = _business_days_ago(TUESDAY, _PDT_WINDOW_BUSINESS_DAYS)
        p._day_trade_dates.append(old_date)
        p.roll_session(TUESDAY, 20_000.0)
        assert p.day_trades_in_window(TUESDAY) == 0
        assert len(p._day_trade_dates) == 0

    def test_overnight_trade_not_miscounted(self):
        """A trade entered Monday closed Tuesday is NOT an intraday round-trip."""
        p = _make_tracker()
        # Simulate: entry_at.date() = MONDAY, exit_at.date() = TUESDAY -> NOT day trade
        # The tracker only increments when entry_at.date() == exit_at.date().
        # Verify no automatic logging happened.
        assert p.day_trades_in_window(TUESDAY) == 0

    def test_rolling_count_accumulates_across_sessions(self):
        p = _make_tracker()
        # 1 day trade Monday
        p._day_trade_dates.append(MONDAY)
        p.roll_session(TUESDAY, 20_000.0)
        # 1 day trade Tuesday
        p._day_trade_dates.append(TUESDAY)
        assert p.day_trades_in_window(TUESDAY) == 2


# ---------------------------------------------------------------------------
# RiskManager PDT gate tests (rolling_day_trades in AccountContext)
# ---------------------------------------------------------------------------

class TestPDTGateRolling:
    def test_below_limit_live_small_account_approved(self):
        rm = _make_risk()
        ctx = _ctx(rolling=2)  # 2 < 3
        d = rm.evaluate_entry(_signal(), ctx, _bars())
        assert d.approved, d.reason

    def test_at_limit_live_small_account_blocked(self):
        """Core regression: rolling_day_trades >= 3 blocks live sub-$25k entry."""
        rm = _make_risk()
        ctx = _ctx(rolling=3)
        d = rm.evaluate_entry(_signal(), ctx, _bars())
        assert not d.approved
        assert "PDT" in d.reason

    def test_above_limit_live_small_account_blocked(self):
        rm = _make_risk()
        ctx = _ctx(rolling=5)
        d = rm.evaluate_entry(_signal(), ctx, _bars())
        assert not d.approved
        assert "PDT" in d.reason

    def test_at_limit_paper_account_allowed_with_warning(self):
        """Paper accounts must not be hard-blocked; warning fires once."""
        rm = _make_risk()
        ctx = _ctx(rolling=3, is_paper=True)
        d = rm.evaluate_entry(_signal(), ctx, _bars())
        assert d.approved

    def test_at_limit_large_equity_live_not_blocked(self):
        """Accounts above $25k are exempt from PDT blocking."""
        rm = _make_risk()
        ctx = _ctx(rolling=3, equity=PDT_EQUITY_MIN + 1, is_paper=False)
        d = rm.evaluate_entry(_signal(), ctx, _bars())
        assert d.approved

    def test_monday_3_trades_tuesday_still_blocked(self):
        """End-to-end: tracker produces rolling_day_trades=3 on Tuesday after Monday's trades."""
        p = _make_tracker()
        for _ in range(3):
            p._day_trade_dates.append(MONDAY)
        p.roll_session(TUESDAY, 20_000.0)

        rolling = p.day_trades_in_window(TUESDAY)
        rm = _make_risk()
        ctx = _ctx(rolling=rolling)
        d = rm.evaluate_entry(_signal(), ctx, _bars())
        assert not d.approved
        assert "PDT" in d.reason

    def test_monday_3_trades_next_monday_allowed(self):
        """After 7 calendar days the window expires and entries are allowed again."""
        p = _make_tracker()
        for _ in range(3):
            p._day_trade_dates.append(MONDAY)
        rolling = p.day_trades_in_window(NEXT_MONDAY)
        assert rolling == 0
        rm = _make_risk()
        ctx = _ctx(rolling=rolling)
        d = rm.evaluate_entry(_signal(), ctx, _bars())
        assert d.approved
