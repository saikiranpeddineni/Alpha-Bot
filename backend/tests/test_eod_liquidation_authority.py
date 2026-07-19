from __future__ import annotations
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
