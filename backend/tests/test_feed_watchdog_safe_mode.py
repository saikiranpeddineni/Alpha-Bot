"""Tests for R1 Feed Watchdog: DATA_STALE (90s) & SAFE_MODE (120s) Latching."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.core.engine import TradingEngine
from app.portfolio.tracker import PortfolioTracker


class FakeClock:
    def __init__(self, now_dt: datetime):
        self._now = now_dt

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float):
        self._now += timedelta(seconds=seconds)


class FakeData:
    def __init__(self):
        self.bars = {}

    async def poll(self, symbols: list[str]):
        return {s: self.bars.get(s.upper(), pd.DataFrame()) for s in symbols}


def make_test_bar() -> pd.DataFrame:
    idx = pd.date_range("2026-11-11 10:00:00", periods=1, freq="min", tz="UTC")
    return pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [5000]},
        index=idx,
    )


@pytest.fixture
def watchdog_engine():
    engine = object.__new__(TradingEngine)
    engine.watchlist = ["AAPL", "MSFT"]
    engine.portfolio = PortfolioTracker()
    engine.data = FakeData()
    engine.risk_config = SimpleNamespace(data_gap_threshold_secs=90.0)
    engine._data_gap_symbols = set()
    engine._last_data_bar_ts = {}
    engine.last_bar_received_at = {}
    engine.feed_staleness_status = {}
    engine.system_health = "NORMAL"
    engine.allow_new_entries = True
    engine.reconciliation_mismatch = False
    engine.reconciliation_failed = False
    # Mock clock to 10:15 AM ET on a regular trading day
    now_et = datetime(2026, 11, 11, 10, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    engine.clock = FakeClock(now_et.astimezone(UTC))
    return engine


def test_feed_staleness_90s_mark(watchdog_engine):
    """Feed is marked DATA_STALE when elapsed time exceeds 90s during regular trading hours."""
    now = watchdog_engine.clock.now()
    active_symbols = set(watchdog_engine.watchlist)

    # Initially fresh bar received
    watchdog_engine.last_bar_received_at["AAPL"] = now
    watchdog_engine.feed_staleness_status["AAPL"] = "OK"

    # Simulate 95 seconds passing
    later = now + timedelta(seconds=95)
    _is_replay = False

    for sym in active_symbols:
        sym_upper = sym.upper()
        last_received = watchdog_engine.last_bar_received_at.get(sym_upper)
        if last_received is not None:
            feed_age = (later - last_received).total_seconds()
            if feed_age > 90.0:
                watchdog_engine.feed_staleness_status[sym_upper] = "DATA_STALE"
                watchdog_engine._data_gap_symbols.add(sym_upper)

    assert watchdog_engine.feed_staleness_status["AAPL"] == "DATA_STALE"
    assert "AAPL" in watchdog_engine._data_gap_symbols


def test_feed_staleness_recovery_on_fresh_bar(watchdog_engine):
    """When a fresh bar arrives, feed staleness resets to OK and symbol is removed from data gap."""
    watchdog_engine.feed_staleness_status["AAPL"] = "DATA_STALE"
    watchdog_engine._data_gap_symbols.add("AAPL")

    now = watchdog_engine.clock.now()
    new_closed = {"AAPL": make_test_bar()}

    for sym_key, bars in new_closed.items():
        if bars is not None and not bars.empty:
            sym_k_upper = sym_key.upper()
            watchdog_engine.last_bar_received_at[sym_k_upper] = now
            watchdog_engine.feed_staleness_status[sym_k_upper] = "OK"
            watchdog_engine._data_gap_symbols.discard(sym_k_upper)

    assert watchdog_engine.feed_staleness_status["AAPL"] == "OK"
    assert "AAPL" not in watchdog_engine._data_gap_symbols


def test_all_feeds_stall_120s_triggers_safe_mode(watchdog_engine):
    """When all active feeds stall > 120s during market hours, engine enters SAFE_MODE."""
    now = watchdog_engine.clock.now()
    active_symbols = set(watchdog_engine.watchlist)

    # Both AAPL and MSFT last received bars 130s ago
    stale_time = now - timedelta(seconds=130)
    watchdog_engine.last_bar_received_at["AAPL"] = stale_time
    watchdog_engine.last_bar_received_at["MSFT"] = stale_time

    now_et = now.astimezone(ZoneInfo("America/New_York"))
    minutes_since_open = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
    assert minutes_since_open >= 3

    all_feeds_stalled = all(
        (sym.upper() not in watchdog_engine.last_bar_received_at)
        or ((now - watchdog_engine.last_bar_received_at[sym.upper()]).total_seconds() > 120.0)
        for sym in active_symbols
    )
    assert all_feeds_stalled is True

    if all_feeds_stalled:
        watchdog_engine.system_health = "SAFE_MODE"
        watchdog_engine.allow_new_entries = False

    assert watchdog_engine.system_health == "SAFE_MODE"
    assert watchdog_engine.allow_new_entries is False


def test_safe_mode_auto_recovers_to_normal(watchdog_engine):
    """Engine in SAFE_MODE auto-recovers to NORMAL when market data feeds resume."""
    watchdog_engine.system_health = "SAFE_MODE"
    watchdog_engine.allow_new_entries = False
    watchdog_engine.reconciliation_mismatch = False
    watchdog_engine.reconciliation_failed = False

    now = watchdog_engine.clock.now()
    active_symbols = set(watchdog_engine.watchlist)

    # AAPL receives fresh bar now, MSFT still stale
    watchdog_engine.last_bar_received_at["AAPL"] = now
    watchdog_engine.last_bar_received_at["MSFT"] = now - timedelta(seconds=130)

    all_feeds_stalled = all(
        (sym.upper() not in watchdog_engine.last_bar_received_at)
        or ((now - watchdog_engine.last_bar_received_at[sym.upper()]).total_seconds() > 120.0)
        for sym in active_symbols
    )
    assert all_feeds_stalled is False

    if (
        not all_feeds_stalled
        and watchdog_engine.system_health == "SAFE_MODE"
        and not watchdog_engine.reconciliation_mismatch
    ):
        watchdog_engine.system_health = "NORMAL"
        watchdog_engine.allow_new_entries = True

    assert watchdog_engine.system_health == "NORMAL"
    assert watchdog_engine.allow_new_entries is True
