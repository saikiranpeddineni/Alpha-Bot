from datetime import datetime, timezone

import pytest

from app.core.calendar_helper import ET, get_session_close


def test_regular_session_closes_at_1600_et():
    entry = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
    close = get_session_close(entry)
    assert close.astimezone(ET).hour == 16
    assert close.astimezone(ET).minute == 0


def test_independence_day_early_close_is_calendar_derived():
    # 2026-07-03 is the observed Independence Day holiday, so July 2 is the
    # XNYS early-close session for that year.
    entry = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
    close = get_session_close(entry)
    assert close.astimezone(ET).hour == 13


def test_day_after_thanksgiving_early_close():
    entry = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)
    close = get_session_close(entry)
    assert close.astimezone(ET).hour == 13


def test_exchange_holiday_fails_closed():
    entry = datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="not an XNYS trading session"):
        get_session_close(entry)


def test_weekend_fails_closed():
    entry = datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="not an XNYS trading session"):
        get_session_close(entry)
