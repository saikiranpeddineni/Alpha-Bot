"""Continuously reconcile local lots to broker-authoritative positions.

Idempotent applicator: accepts legacy, Alpaca-scoped hardened, and final Broker-interface
hardened forms. Unknown drift still fails closed. Also keeps the generated regression
aligned with the final Broker-interface architecture.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
TEST = ROOT / "backend/tests/test_tick_reconciliation_authority_hardening_20260729.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''            account = await self.broker.account()\n            positions = await self.broker.positions()\n            if getattr(self, "reconciliation_failed", False):\n                applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")\n                self._reconcile_positions(positions)\n            if positions and not getattr(self, "reconciliation_failed", False):\n                await self.verify_broker_protective_stops(positions)\n'''
intermediate = '''            account = await self.broker.account()\n            positions = await self.broker.positions()\n\n            # Fill deltas were drained immediately before this snapshot.  Broker\n            # positions are now the authoritative exposure boundary: continuously\n            # repair local lots so an API/stream visibility gap or external/manual\n            # execution cannot leave sizing, exits, or Rule-41 operating on stale\n            # quantity.  _reconcile_positions does not create realized trades, and\n            # durable fill watermarks keep a later replay of an already-accounted\n            # cumulative fill idempotent.\n            from app.brokers.alpaca import AlpacaBroker\n            if isinstance(self.broker, AlpacaBroker):\n                if getattr(self, "reconciliation_failed", False):\n                    applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")\n                self._reconcile_positions(positions)\n\n            if positions and not getattr(self, "reconciliation_failed", False):\n                await self.verify_broker_protective_stops(positions)\n'''
final = '''            account = await self.broker.account()\n            positions = await self.broker.positions()\n\n            # Fill deltas were drained immediately before this snapshot. Broker\n            # positions are the authoritative exposure boundary for every Broker\n            # implementation; reconciliation must not depend on a concrete adapter.\n            if getattr(self, "reconciliation_failed", False):\n                applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")\n            self._reconcile_positions(positions)\n\n            if positions and not getattr(self, "reconciliation_failed", False):\n                await self.verify_broker_protective_stops(positions)\n'''

if final not in src:
    if intermediate in src:
        src = src.replace(intermediate, final, 1)
    elif old in src:
        src = src.replace(old, final, 1)
    else:
        pos_idx = src.find('positions = await self.broker.positions()')
        verify_idx = src.find('await self.verify_broker_protective_stops(positions)', pos_idx)
        region = src[pos_idx:verify_idx] if pos_idx >= 0 and verify_idx >= 0 else ''
        if 'self._reconcile_positions(positions)' not in region or 'isinstance(self.broker, AlpacaBroker)' in region:
            raise SystemExit("tick reconciliation authority is in an unknown state; refusing unsafe patch")

if src != original:
    ENGINE.write_text(src, encoding="utf-8")
    print("Applied continuous broker-authoritative tick reconciliation")
else:
    print("Continuous broker-authoritative tick reconciliation already applied")

# The final architecture intentionally reconciles every Broker implementation. A stale
# generated test used to require an Alpaca concrete-class guard, contradicting that
# contract and the recovery tests that use broker-compatible wrappers/fakes.
if TEST.exists():
    test_src = TEST.read_text(encoding="utf-8")
    stale = '''def test_alpaca_positions_reconcile_even_without_prior_failure():\n    body = _tick_source()\n    positions = body.index("positions = await self.broker.positions()")\n    protection = body.index("await self.verify_broker_protective_stops(positions)", positions)\n    block = body[positions:protection]\n    assert "if isinstance(self.broker, AlpacaBroker):" in block\n    assert "self._reconcile_positions(positions)" in block\n'''
    aligned = '''def test_broker_positions_reconcile_even_without_prior_failure():\n    body = _tick_source()\n    positions = body.index("positions = await self.broker.positions()")\n    protection = body.index("await self.verify_broker_protective_stops(positions)", positions)\n    block = body[positions:protection]\n    assert "self._reconcile_positions(positions)" in block\n    assert "if isinstance(self.broker, AlpacaBroker):" not in block\n'''
    if stale in test_src:
        TEST.write_text(test_src.replace(stale, aligned, 1), encoding="utf-8")
