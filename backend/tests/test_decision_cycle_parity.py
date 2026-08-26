from __future__ import annotations

import inspect

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
