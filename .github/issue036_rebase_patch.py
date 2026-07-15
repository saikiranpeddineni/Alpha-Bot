from pathlib import Path
p = Path('backend/app/core/engine.py')
t = p.read_text()
marker = '''    # ------------------------------------------------------------------\n    # the tick\n    # ------------------------------------------------------------------\n\n    async def _tick(self) -> None:\n'''
helper = '''    # ------------------------------------------------------------------\n    # authoritative EOD liquidation\n    # ------------------------------------------------------------------\n\n    async def _reconcile_eod_liquidation(self, session, now: datetime) -> bool:  # noqa: ANN001\n        \"\"\"Retry EOD liquidation until the broker proves positions and orders are clear.\"\"\"\n        assert self.broker is not None\n        try:\n            open_orders = await self.broker.open_orders()\n            if open_orders:\n                applog.warning(\"engine\", f\"EOD liquidation: cancelling {len(open_orders)} open orders before flatten retry\")\n                await self.broker.cancel_all()\n            positions = await self.broker.positions()\n            if positions:\n                applog.warning(\"engine\", f\"EOD liquidation unresolved: flattening {len(positions)} authoritative positions\")\n                await self.broker.flatten_all(reason=\"end of day\")\n                await self._drain_fills(now)\n            residual_positions = await self.broker.positions()\n            residual_orders = await self.broker.open_orders()\n            if residual_positions or residual_orders:\n                symbols = sorted({position.symbol for position in residual_positions})\n                self.last_error = (\n                    \"CRITICAL EOD liquidation unresolved: \"\n                    f\"residual_positions={symbols}, open_orders={len(residual_orders)}\"\n                )\n                applog.error(\"engine\", self.last_error)\n                self.publish(\"status\", self.status())\n                return False\n            self._flattened_sessions.add(session)\n            if self.last_error.startswith(\"CRITICAL EOD liquidation unresolved\"):\n                self.last_error = \"\"\n            applog.info(\"engine\", \"End-of-day liquidation authoritatively complete\")\n            self.publish(\"status\", self.status())\n            return True\n        except BaseException as exc:\n            self.last_error = f\"CRITICAL EOD liquidation unresolved: {type(exc).__name__}: {exc}\"\n            applog.error(\"engine\", self.last_error)\n            self.publish(\"status\", self.status())\n            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):\n                raise\n            return False\n\n    # ------------------------------------------------------------------\n    # the tick\n    # ------------------------------------------------------------------\n\n    async def _tick(self) -> None:\n'''
assert marker in t
t = t.replace(marker, helper, 1)
old = '''        # 4. end-of-day flatten\n        if (\n            self.state == EngineState.RUNNING\n            and positions\n            and self.risk.should_flatten(now)\n            and session not in self._flattened_sessions\n        ):\n            self._flattened_sessions.add(session)\n            applog.info(\"engine\", \"End-of-day window -- flattening all positions\")\n            await self.broker.flatten_all(reason=\"end of day\")\n            await self._drain_fills(now)\n\n        # 5. strategy evaluation on newly closed bars\n        can_trade = (\n            self.state == EngineState.RUNNING\n            and not self.risk.daily_halted\n            and (self.clock.is_open(now) or self.mode == \"demo\")\n        )\n'''
new = '''        # 4. end-of-day flatten. Completion is latched only after an\n        # authoritative broker post-condition proves no positions or open orders.\n        in_flatten_window = self.risk.should_flatten(now)\n        if (\n            self.state == EngineState.RUNNING\n            and in_flatten_window\n            and session not in self._flattened_sessions\n        ):\n            await self._reconcile_eod_liquidation(session, now)\n\n        # 5. strategy evaluation on newly closed bars. Do not reopen exposure\n        # while EOD liquidation is active, including unresolved retry ticks.\n        can_trade = (\n            self.state == EngineState.RUNNING\n            and not self.risk.daily_halted\n            and not in_flatten_window\n            and (self.clock.is_open(now) or self.mode == \"demo\")\n        )\n'''
assert old in t
p.write_text(t.replace(old, new, 1))
Path('backend/tests/test_eod_liquidation_authority.py').write_text(r'''from __future__ import annotations
import asyncio
from types import SimpleNamespace
import pytest
from app.core.engine import TradingEngine

class BrokerDouble:
    name = "double"
    is_paper = False
    def __init__(self, position_batches, order_batches=None, *, flatten_error=None):
        self.position_batches = list(position_batches)
        self.order_batches = list(order_batches or [[]])
        self.flatten_error = flatten_error
        self.flatten_calls = 0
        self.cancel_calls = 0
    async def positions(self):
        batch = self.position_batches.pop(0) if len(self.position_batches) > 1 else self.position_batches[0]
        return [SimpleNamespace(symbol=s, qty=q) for s, q in batch]
    async def open_orders(self):
        batch = self.order_batches.pop(0) if len(self.order_batches) > 1 else self.order_batches[0]
        return [SimpleNamespace(id=str(i), symbol=s) for i, s in enumerate(batch)]
    async def cancel_all(self): self.cancel_calls += 1; return 1
    async def flatten_all(self, reason="flatten"):
        self.flatten_calls += 1
        if self.flatten_error: raise self.flatten_error
        return []

def engine_for(broker):
    engine = object.__new__(TradingEngine)
    engine.broker = broker
    engine._flattened_sessions = set()
    engine.last_error = ""
    engine.publish = lambda *_: None
    engine.status = lambda: {"last_error": engine.last_error}
    async def drain(_now): return None
    engine._drain_fills = drain
    return engine

def run(coro): return asyncio.run(coro)

def test_provider_exception_stays_unresolved_and_retry_eligible():
    broker = BrokerDouble([[('AAPL', 10)]], flatten_error=RuntimeError('provider down'))
    engine = engine_for(broker)
    assert run(engine._reconcile_eod_liquidation('S1', None)) is False
    assert 'S1' not in engine._flattened_sessions
    assert 'CRITICAL EOD liquidation unresolved' in engine.last_error

def test_failed_attempt_then_recovery_latches_only_after_authoritative_flat():
    broker = BrokerDouble([[('AAPL', 10)]], flatten_error=RuntimeError('timeout'))
    engine = engine_for(broker)
    assert run(engine._reconcile_eod_liquidation('S1', None)) is False
    broker.flatten_error = None
    broker.position_batches = [[('AAPL', 10)], []]
    assert run(engine._reconcile_eod_liquidation('S1', None)) is True
    assert engine._flattened_sessions == {'S1'}
    assert engine.last_error == ''

def test_immediate_flat_state_latches_without_flatten_submission():
    broker = BrokerDouble([[]]); engine = engine_for(broker)
    assert run(engine._reconcile_eod_liquidation('S1', None)) is True
    assert broker.flatten_calls == 0

@pytest.mark.parametrize('qty', [10, -10])
def test_pending_or_partial_liquidation_never_claims_completion(qty):
    broker = BrokerDouble([[("AAPL", qty)], [("AAPL", qty / 2)]]); engine = engine_for(broker)
    assert run(engine._reconcile_eod_liquidation('S1', None)) is False
    assert broker.flatten_calls == 1
    assert 'S1' not in engine._flattened_sessions

def test_multiple_symbols_require_zero_authoritative_residual_exposure():
    broker = BrokerDouble([[('AAPL', 10), ('MSFT', -5)], [('MSFT', -2)]]); engine = engine_for(broker)
    assert run(engine._reconcile_eod_liquidation('S1', None)) is False
    assert 'MSFT' in engine.last_error

def test_open_orders_are_cancelled_and_must_reconcile_empty_before_completion():
    broker = BrokerDouble([[]], [['AAPL'], ['AAPL']]); engine = engine_for(broker)
    assert run(engine._reconcile_eod_liquidation('S1', None)) is False
    assert broker.cancel_calls == 1
    assert 'open_orders=1' in engine.last_error

def test_cancelled_liquidation_propagates_and_does_not_latch():
    broker = BrokerDouble([[('AAPL', 10)]], flatten_error=asyncio.CancelledError()); engine = engine_for(broker)
    with pytest.raises(asyncio.CancelledError): run(engine._reconcile_eod_liquidation('S1', None))
    assert 'S1' not in engine._flattened_sessions
''')
