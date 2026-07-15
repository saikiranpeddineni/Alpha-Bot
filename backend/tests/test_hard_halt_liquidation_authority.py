from __future__ import annotations
import asyncio
from types import SimpleNamespace
import pytest
from app.core.engine import TradingEngine

class BrokerDouble:
    name='double'; is_paper=False
    def __init__(self, positions, orders=None, error=None):
        self.position_batches=list(positions); self.order_batches=list(orders or [[]]); self.error=error; self.flatten_calls=0; self.cancel_calls=0
    async def positions(self):
        b=self.position_batches.pop(0) if len(self.position_batches)>1 else self.position_batches[0]
        return [SimpleNamespace(symbol=s,qty=q) for s,q in b]
    async def open_orders(self):
        b=self.order_batches.pop(0) if len(self.order_batches)>1 else self.order_batches[0]
        return [SimpleNamespace(symbol=s,id=str(i)) for i,s in enumerate(b)]
    async def cancel_all(self): self.cancel_calls+=1; return 1
    async def flatten_all(self, reason=''): self.flatten_calls+=1; (_ for _ in ()).throw(self.error) if self.error else None; return []

def eng(b):
    e=object.__new__(TradingEngine); e.broker=b; e.last_error=''; e.publish=lambda *_:None; e.status=lambda:{'last_error':e.last_error}
    async def drain(_): return None
    e._drain_fills=drain; return e

def run(c): return asyncio.run(c)

def test_provider_failure_remains_unresolved_then_retries_after_recovery():
    b=BrokerDouble([[('AAPL',10)]],error=RuntimeError('provider down')); e=eng(b)
    assert run(e._reconcile_hard_halt_liquidation(None)) is False
    assert 'liquidation unresolved' in e.last_error
    b.error=None; b.position_batches=[[('AAPL',10)],[]]
    assert run(e._reconcile_hard_halt_liquidation(None)) is True
    assert e.last_error=='HARD HALTED — flat'

def test_immediate_flat_is_authoritative_and_does_not_submit_exit():
    b=BrokerDouble([[]]); e=eng(b)
    assert run(e._reconcile_hard_halt_liquidation(None)) is True
    assert b.flatten_calls==0

@pytest.mark.parametrize('qty',[10,-10])
def test_partial_long_and_short_remain_unresolved_without_false_completion(qty):
    b=BrokerDouble([[('AAPL',qty)],[('AAPL',qty/2)]]); e=eng(b)
    assert run(e._reconcile_hard_halt_liquidation(None)) is False
    assert b.flatten_calls==1

def test_multiple_symbols_all_must_reconcile_flat():
    b=BrokerDouble([[('AAPL',10),('MSFT',-5)],[('MSFT',-2)]]); e=eng(b)
    assert run(e._reconcile_hard_halt_liquidation(None)) is False
    assert 'MSFT' in e.last_error

def test_open_orders_cancelled_and_must_be_authoritatively_empty():
    b=BrokerDouble([[]],[['AAPL'],['AAPL']]); e=eng(b)
    assert run(e._reconcile_hard_halt_liquidation(None)) is False
    assert b.cancel_calls==1 and 'open_orders=1' in e.last_error

def test_cancelled_reconciliation_propagates_and_never_claims_flat():
    b=BrokerDouble([[('AAPL',10)]],error=asyncio.CancelledError()); e=eng(b)
    with pytest.raises(asyncio.CancelledError): run(e._reconcile_hard_halt_liquidation(None))
    assert 'liquidation unresolved' in e.last_error
