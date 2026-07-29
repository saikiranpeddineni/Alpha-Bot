"""Continuously reconcile local lots to broker-authoritative positions.

The engine already drains broker fill deltas before fetching positions.  However,
_reconcile_positions was only invoked at startup or after a prior reconciliation
failure.  That leaves no normal-cycle repair path when the broker position changes
without a locally drainable fill (stream/API visibility gap, external/manual fill,
or restart timing edge).

After fill draining, broker positions are the authoritative exposure snapshot.
Reconcile local lots on every successful live/paper Alpaca position snapshot before
Rule-41 protection verification.  Durable fill watermarks still make later fill
replays idempotent; reconciliation itself must not manufacture realized trades.

Run from repository root:
    python tools/apply_tick_reconciliation_authority_hardening_20260729.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''            account = await self.broker.account()\n            positions = await self.broker.positions()\n            if getattr(self, "reconciliation_failed", False):\n                applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")\n                self._reconcile_positions(positions)\n            if positions and not getattr(self, "reconciliation_failed", False):\n                await self.verify_broker_protective_stops(positions)\n'''
new = '''            account = await self.broker.account()\n            positions = await self.broker.positions()\n\n            # Fill deltas were drained immediately before this snapshot.  Broker\n            # positions are now the authoritative exposure boundary: continuously\n            # repair local lots so an API/stream visibility gap or external/manual\n            # execution cannot leave sizing, exits, or Rule-41 operating on stale\n            # quantity.  _reconcile_positions does not create realized trades, and\n            # durable fill watermarks keep a later replay of an already-accounted\n            # cumulative fill idempotent.\n            from app.brokers.alpaca import AlpacaBroker\n            if isinstance(self.broker, AlpacaBroker):\n                if getattr(self, "reconciliation_failed", False):\n                    applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")\n                self._reconcile_positions(positions)\n\n            if positions and not getattr(self, "reconciliation_failed", False):\n                await self.verify_broker_protective_stops(positions)\n'''
if old not in src:
    raise SystemExit("tick reconciliation anchor not found; refusing unsafe patch")
src = src.replace(old, new, 1)

if src == original:
    raise SystemExit("no changes applied")
ENGINE.write_text(src, encoding="utf-8")
print("Applied continuous broker-authoritative tick reconciliation")
