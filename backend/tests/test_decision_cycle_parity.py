from __future__ import annotations

import inspect

from app.backtest import engine as backtest_engine
from app.core.engine import TradingEngine


def _ordered(source: str, *needles: str) -> None:
    offsets = [source.index(needle) for needle in needles]
    assert offsets == sorted(offsets), list(zip(needles, offsets))


def test_live_decision_cycle_awaits_scanner_and_event_state_before_poll_and_decisions():
    source = inspect.getsource(TradingEngine._tick)
    assert "create_task(self.run_market_scanner" not in source
    assert "create_task(self.run_event_monitor" in source
    loop = source[source.index("Pre-market scanner trigger time reached"):]
    _ordered(loop, "self.run_market_scanner(is_intraday=False)", "create_task(self.run_event_monitor(now), name=\"event-monitor\")", "new_closed = await self.data.poll(", "await self._drain_fills(now)", "self.portfolio.roll_session(session, account.equity)", "halt = self.risk.check_halts(ctx, now=now)", "entries = await self._evaluate_symbol")


def test_replay_processes_and_drains_boundary_fills_before_session_roll_and_decisions():
    source = inspect.getsource(backtest_engine.run_backtest)
    loop = source[source.index("for step, ts in enumerate(all_ts):") :]
    _ordered(loop, "outcome, scanned = await scanner.run(as_of=now)", "is_event, reason = await calendar.check(now, current_watchlist)", "broker.process_bar(symbol, frames[symbol].iloc[i], now)", "drain(now)", "portfolio.roll_session(session, broker.equity())", "ctx = account_ctx(now)", "halt = risk.check_halts(ctx, now=now)", "candidates_this_tick = []")
