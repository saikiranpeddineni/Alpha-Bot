from pathlib import Path
p=Path('backend/app/core/engine.py')
t=p.read_text()
marker='''    # ------------------------------------------------------------------\n    # authoritative EOD liquidation\n    # ------------------------------------------------------------------\n'''
helper='''    # ------------------------------------------------------------------\n    # authoritative hard-halt liquidation\n    # ------------------------------------------------------------------\n\n    async def _reconcile_hard_halt_liquidation(self, now: datetime) -> bool:\n        \"\"\"Keep a latched hard halt unresolved until broker state proves flat.\"\"\"\n        assert self.broker is not None\n        try:\n            open_orders = await self.broker.open_orders()\n            if open_orders:\n                applog.warning(\"engine\", f\"Hard halt liquidation: cancelling {len(open_orders)} open orders\")\n                await self.broker.cancel_all()\n            positions = await self.broker.positions()\n            if positions:\n                applog.warning(\"engine\", f\"Hard halt liquidation unresolved: flattening {len(positions)} authoritative positions\")\n                await self.broker.flatten_all(reason=\"max drawdown kill switch\")\n                await self._drain_fills(now)\n            residual_positions = await self.broker.positions()\n            residual_orders = await self.broker.open_orders()\n            if residual_positions or residual_orders:\n                symbols = sorted({position.symbol for position in residual_positions})\n                self.last_error = (\n                    \"HARD HALTED — liquidation unresolved: \"\n                    f\"residual_positions={symbols}, open_orders={len(residual_orders)}\"\n                )\n                applog.error(\"engine\", self.last_error)\n                self.publish(\"status\", self.status())\n                return False\n            if self.last_error.startswith(\"HARD HALTED — liquidation unresolved\"):\n                self.last_error = \"HARD HALTED — flat\"\n            applog.info(\"engine\", \"Hard halt liquidation authoritatively complete: flat\")\n            self.publish(\"status\", self.status())\n            return True\n        except BaseException as exc:\n            self.last_error = f\"HARD HALTED — liquidation unresolved: {type(exc).__name__}: {exc}\"\n            applog.error(\"engine\", self.last_error)\n            self.publish(\"status\", self.status())\n            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):\n                raise\n            return False\n\n'''
assert marker in t
t=t.replace(marker,helper+marker,1)
old='''        halt = self.risk.check_halts(ctx, now=now)\n        if halt == \"hard\":\n            await self.broker.flatten_all(reason=\"max drawdown kill switch\")\n            await self._drain_fills(now)\n            self.state = EngineState.HALTED\n            self.insights.on_risk_event(\n                \"Kill switch tripped\", self.risk.hard_halt_reason, severity=\"danger\"\n            )\n            self.publish(\"status\", self.status())\n'''
new='''        halt = self.risk.check_halts(ctx, now=now)\n        if halt == \"hard\":\n            # Latch the capital-preservation state before any fallible broker I/O.\n            self.state = EngineState.HALTED\n            self.insights.on_risk_event(\n                \"Kill switch tripped\", self.risk.hard_halt_reason, severity=\"danger\"\n            )\n            await self._reconcile_hard_halt_liquidation(now)\n            self.publish(\"status\", self.status())\n'''
assert old in t
t=t.replace(old,new,1)
# A persisted/restarted hard halt must continue reconciliation even though check_halts may not emit a new edge.
needle='''        elif halt == \"daily\":\n'''
t=t.replace(needle,'''        elif self.state == EngineState.HALTED and self.risk.hard_halted:\n            await self._reconcile_hard_halt_liquidation(now)\n        elif halt == \"daily\":\n''',1)
p.write_text(t)

Path('backend/tests/test_hard_halt_liquidation_authority.py').write_text(r'''from __future__ import annotations
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
''')
