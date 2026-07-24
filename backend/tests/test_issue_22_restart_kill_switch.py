from datetime import date

from app.config import RiskConfig
from app.core.clock import MarketClock
from app.portfolio.tracker import PortfolioTracker
from app.risk.manager import RiskManager


def _risk() -> RiskManager:
    return RiskManager(RiskConfig(), MarketClock(always_open=True))


def test_daily_halt_round_trip_preserves_halt_date() -> None:
    original = _risk()
    original.daily_halted = True
    original.daily_halt_date = date(2026, 7, 24)

    restored = _risk()
    restored.restore_risk_state(original.get_risk_state())

    assert restored.daily_halted is True
    assert restored.daily_halt_date == date(2026, 7, 24)


def test_same_session_restart_does_not_clear_daily_halt() -> None:
    risk = _risk()
    risk.restore_risk_state({
        "daily_halted": True,
        "daily_halt_date": "2026-07-24",
    })

    risk.roll_session(date(2026, 7, 24))

    assert risk.daily_halted is True
    assert risk.daily_halt_date == date(2026, 7, 24)


def test_stale_or_backward_session_does_not_clear_daily_halt() -> None:
    risk = _risk()
    risk.restore_risk_state({
        "daily_halted": True,
        "daily_halt_date": "2026-07-24",
    })

    risk.roll_session(date(2026, 7, 23))

    assert risk.daily_halted is True
    assert risk.daily_halt_date == date(2026, 7, 24)


def test_forward_session_clears_daily_halt_only() -> None:
    risk = _risk()
    risk.restore_risk_state({
        "daily_halted": True,
        "daily_halt_date": "2026-07-24",
        "hard_halted": True,
        "hard_halt_reason": "max drawdown",
    })

    risk.roll_session(date(2026, 7, 25))

    assert risk.daily_halted is False
    assert risk.daily_halt_date is None
    assert risk.hard_halted is True
    assert risk.hard_halt_reason == "max drawdown"


def test_hard_halt_round_trip_preserves_reason() -> None:
    original = _risk()
    original.hard_halted = True
    original.hard_halt_reason = "Max drawdown breached"

    restored = _risk()
    restored.restore_risk_state(original.get_risk_state())

    assert restored.hard_halted is True
    assert restored.hard_halt_reason == "Max drawdown breached"


def test_missing_daily_halt_date_fails_closed_on_roll() -> None:
    risk = _risk()
    risk.restore_risk_state({"daily_halted": True})

    risk.roll_session(date(2026, 7, 25))

    assert risk.daily_halted is True
    assert risk.daily_halt_date is None


def test_same_session_restart_must_not_rebase_restored_peak_equity() -> None:
    """Lock the remaining #22 defect: restart equity must not erase drawdown history."""
    portfolio = PortfolioTracker()
    portfolio.restore_checkpoint({
        "session_date": "2026-07-24",
        "session_start_equity": 100_000.0,
        "peak_equity": 105_000.0,
        "trades": [],
        "equity_curve": [],
        "lots": {},
    })

    # Mirrors TradingEngine.start() on a same-session restart at post-loss equity.
    portfolio.roll_session(date(2026, 7, 24), 97_000.0)
    portfolio.peak_equity = 97_000.0

    assert portfolio.session_start_equity == 100_000.0
    assert portfolio.peak_equity == 105_000.0
