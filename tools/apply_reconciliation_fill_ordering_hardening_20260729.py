"""Harden broker/local lot reconciliation against transient retry races.

Idempotent applicator: accepts an already-materialized reconciliation implementation
while failing closed when neither known form exists.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''    def _reconcile_positions(self, positions: list[Position]) -> None:\n        """Reconcile and reconstruct database portfolio lot states from broker positions."""\n        self.portfolio._lots.clear()\n        for p in positions:\n            strat = "manual"'''
new = '''    def _reconcile_positions(self, positions: list[Position]) -> None:\n        """Reconcile local lots to authoritative broker positions without erasing matching lot state."""\n        broker_symbols = {p.symbol for p in positions if abs(float(p.qty)) > 1e-9}\n        for local_symbol in list(self.portfolio._lots):\n            if local_symbol not in broker_symbols:\n                self.portfolio._lots.pop(local_symbol, None)\n                self._position_take_profits.pop(local_symbol, None)\n                self._position_peaks.pop(local_symbol, None)\n\n        for p in positions:\n            existing_lot = self.portfolio._lots.get(p.symbol)\n            if existing_lot is not None:\n                qty_matches = abs(float(existing_lot.qty) - float(p.qty)) <= 1e-9\n                price_scale = max(1.0, abs(float(p.avg_entry_price)))\n                avg_matches = abs(float(existing_lot.avg_price) - float(p.avg_entry_price)) <= 1e-8 * price_scale\n                if qty_matches and avg_matches:\n                    # Fill processing already established the same economic lot. Keep\n                    # its original/current stop, initial R, strategy and entry timestamp.\n                    p.strategy = existing_lot.strategy\n                    continue\n\n            strat = "manual"'''

if new not in src:
    if old not in src:
        raise SystemExit("reconciliation/fill ordering is neither legacy nor hardened; refusing unsafe patch")
    src = src.replace(old, new, 1)

if src != original:
    ENGINE.write_text(src, encoding="utf-8")
    print("Applied reconciliation/fill ordering hardening to backend/app/core/engine.py")
else:
    print("Reconciliation/fill ordering hardening already applied")
