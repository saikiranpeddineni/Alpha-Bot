"""Regression tests for the tick session-time scope bug (UnboundLocalError).

Root cause: ``now_et``, ``today_date``, and ``et_mins`` were initialized only
inside ``if self.state == EngineState.RUNNING:`` but the market-data-gap /
regular-session detection block consumed those names unconditionally.

In any non-RUNNING state (SAFE_MODE, STOPPED, etc.) the interpreter raised::

    UnboundLocalError: cannot access local variable 'now_et'
    UnboundLocalError: cannot access local variable 'et_mins'   # secondary

Fix: move the three initializations immediately after ``now = utcnow()``.

Test coverage:
1. RUNNING during regular market session.
2. SAFE_MODE -- primary regression: no UnboundLocalError.
3. Outside regular session (pre-market) -- gap flags cleared.
4. Weekend / market holiday -- ValueError absorbed.
5. get_session_close raises ValueError -- fails closed.
6. Unexpected calendar exception -- et_mins fallback used.
7. Early-close session -- early close respected.
8. Smoke: tick_count increments.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.core.domain import EngineState
from app.core.engine import TradingEngine
from app.db.database import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(tmp_path) -> TradingEngine:
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "tick_scope.db"),
        DEFAULT_WATCHLIST="SPY",
        DEMO_BAR_SECONDS=3600,
        RISK_PER_TRADE_PCT=1.0,
    )
    db = Database(settings.resolved_db_path)
    return TradingEngine(settings, db)


def _stub(engine: TradingEngine) -> None:
    """Attach bare-minimum mocks so _tick() can run without real I/O."""
    broker = MagicMock()
    broker.is_paper = True
    broker.poll_fills = AsyncMock(return_value=[])
    broker.drain_fills = MagicMock(return_value=[])
    broker.positions = AsyncMock(return_value=[])
    broker.open_orders = AsyncMock(return_value=[])
    broker.account = AsyncMock(
        return_value=SimpleNamespace(
            equity=10_000.0, cash=10_000.0, buying_power=40_000.0
        )
    )
    engine.broker = broker

    data = MagicMock()
    data.poll = AsyncMock(return_value={})
    engine.data = data

    risk = MagicMock()
    risk.hard_halted = False
    risk.daily_halted = False
    risk.check_halts = MagicMock(return_value=None)
    risk.should_flatten = MagicMock(return_value=False)
    risk.roll_session = MagicMock()
    engine.risk = risk

    portfolio = MagicMock()
    portfolio.equity = 10_000.0
    portfolio.session_start_equity = 10_000.0
    portfolio.peak_equity = 10_000.0
    portfolio.day_pnl = MagicMock(return_value=0.0)
    portfolio.day_trades_today = 0
    portfolio.day_trades_in_window = MagicMock(return_value=0)
    portfolio.roll_session = MagicMock(return_value=False)
    engine.portfolio = portfolio

    pipeline = MagicMock()
    pipeline.ml_threshold = 0.55
    engine.pipeline = pipeline

    engine.run_market_scanner = AsyncMock()
    engine.run_event_monitor = AsyncMock()
    engine._check_system_heartbeat = AsyncMock()
    engine._reconcile_pending_submissions = AsyncMock()
    engine.shadow_research = MagicMock()
    engine.shadow_research.publish = MagicMock()
    engine.insights = MagicMock()
    engine.publish = MagicMock()
    engine.clock = MagicMock()
    engine.clock.session_date = MagicMock(return_value="2026-01-06")
    engine.clock.is_open = MagicMock(return_value=True)
    engine.trainer = None
    engine.perf_tracker = MagicMock()
    engine.portfolio_gate = None
    engine.portfolio_allocator = None


# Tuesday 2026-01-06 10:30 ET (15:30 UTC) -- confirmed trading session
_SESSION_TS = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. RUNNING during regular market session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_running_regular_session(tmp_path):
    engine = _make_engine(tmp_path)
    _stub(engine)
    engine.state = EngineState.RUNNING
    with patch("app.core.engine.utcnow", return_value=_SESSION_TS):
        await engine._tick()
    assert engine.state == EngineState.RUNNING


# ---------------------------------------------------------------------------
# 2. SAFE_MODE -- primary regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_safe_mode_no_unbound_local_error(tmp_path):
    """SAFE_MODE tick must NOT raise UnboundLocalError.

    Before the fix, now_et/et_mins were only bound inside the RUNNING block.
    Any non-RUNNING tick path hit UnboundLocalError at the gap-detection block.
    The except-Exception fallback then raised a second UnboundLocalError on et_mins.
    """
    engine = _make_engine(tmp_path)
    _stub(engine)
    engine.state = EngineState.SAFE_MODE
    with patch("app.core.engine.utcnow", return_value=_SESSION_TS):
        await engine._tick()
    # State must not be silently promoted to RUNNING
    assert engine.state == EngineState.SAFE_MODE


# ---------------------------------------------------------------------------
# 3. Outside regular session (pre-market)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_outside_session_clears_gap_flags(tmp_path):
    engine = _make_engine(tmp_path)
    _stub(engine)
    engine.state = EngineState.RUNNING
    pre_market = datetime(2026, 1, 6, 13, 0, tzinfo=UTC)  # 08:00 ET
    engine._data_gap_symbols = {"SPY", "NVDA"}
    with patch("app.core.engine.utcnow", return_value=pre_market):
        await engine._tick()
    assert len(engine._data_gap_symbols) == 0


# ---------------------------------------------------------------------------
# 4. Weekend / market holiday
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_weekend_no_error(tmp_path):
    engine = _make_engine(tmp_path)
    _stub(engine)
    engine.state = EngineState.RUNNING
    saturday = datetime(2026, 1, 10, 15, 30, tzinfo=UTC)  # Saturday 10:30 ET
    with patch("app.core.engine.utcnow", return_value=saturday):
        await engine._tick()
    assert engine._data_gap_symbols == set()


# ---------------------------------------------------------------------------
# 5. get_session_close raises ValueError -- fail closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_session_close_value_error_fails_closed(tmp_path):
    engine = _make_engine(tmp_path)
    _stub(engine)
    engine.state = EngineState.RUNNING
    engine._data_gap_symbols = {"SPY"}
    with (
        patch("app.core.engine.utcnow", return_value=_SESSION_TS),
        patch(
            "app.core.calendar_helper.get_session_close",
            side_effect=ValueError("not a trading session"),
        ),
    ):
        await engine._tick()
    assert "SPY" not in engine._data_gap_symbols


# ---------------------------------------------------------------------------
# 6. Unexpected calendar exception -- et_mins fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_calendar_unexpected_exception_fallback(tmp_path):
    """Non-ValueError from calendar uses et_mins fallback without crashing.

    Before the fix: in SAFE_MODE, et_mins was unbound, so the fallback
    expression also raised UnboundLocalError.
    """
    engine = _make_engine(tmp_path)
    _stub(engine)
    engine.state = EngineState.SAFE_MODE
    with (
        patch("app.core.engine.utcnow", return_value=_SESSION_TS),
        patch(
            "app.core.calendar_helper.get_session_close",
            side_effect=RuntimeError("network failure"),
        ),
    ):
        await engine._tick()  # must not raise


# ---------------------------------------------------------------------------
# 7. Early-close session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_early_close_session(tmp_path):
    """After an early close the session gate recognises out-of-session."""
    from zoneinfo import ZoneInfo
    ET_tz = ZoneInfo("America/New_York")
    engine = _make_engine(tmp_path)
    _stub(engine)
    engine.state = EngineState.RUNNING
    # 13:30 ET -- 30 min after simulated 13:00 early close
    after_close = datetime(2026, 11, 27, 18, 30, tzinfo=UTC)
    early_close_dt = datetime(2026, 11, 27, 18, 0, tzinfo=UTC).astimezone(ET_tz)
    with (
        patch("app.core.engine.utcnow", return_value=after_close),
        patch(
            "app.core.calendar_helper.get_session_close",
            return_value=early_close_dt,
        ),
    ):
        await engine._tick()
    assert engine._data_gap_symbols == set()


# ---------------------------------------------------------------------------
# 8. Smoke: tick_count increments
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_running_smoke_increments_count(tmp_path):
    engine = _make_engine(tmp_path)
    _stub(engine)
    engine.state = EngineState.RUNNING
    initial = engine._tick_count
    with patch("app.core.engine.utcnow", return_value=_SESSION_TS):
        await engine._tick()
    assert engine._tick_count == initial + 1
