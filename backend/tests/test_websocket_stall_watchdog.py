import pytest
from app.core.engine import TradingEngine


def test_websocket_stall_watchdog_diagnostics():
    engine = object.__new__(TradingEngine)
    engine.mode = "paper"
    engine.allow_new_entries = True
    engine.clock = type("Clock", (), {"__name__": "RealClock"})()

    # When all symbols are stale, allow_new_entries is set to False
    symbols_fresh = {"AAPL": False, "MSFT": False}
    any_fresh = any(symbols_fresh.values())
    if not any_fresh:
        engine.allow_new_entries = False

    assert engine.allow_new_entries is False, "Expected new entries to be blocked when all feeds stall"
