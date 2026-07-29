"""Harden broker/local lot reconciliation against transient retry races.

Normal ticks already drain broker fill deltas before fetching authoritative positions.
The remaining dangerous path is a reconciliation retry: _reconcile_positions() clears
all local lots and rebuilds them from broker snapshots.  That is correct for startup,
but destructive during a transient runtime recovery because it discards lot-local
state (entry timestamp, original/current stop, initial R) even when broker quantity
and average price already agree.

This patch makes runtime reconciliation idempotent: preserve an existing lot when its
signed quantity and average entry price match broker truth; reconstruct only missing
or genuinely divergent lots; remove only symbols the broker proves are absent.

Run from repository root:
    python tools/apply_reconciliation_fill_ordering_hardening_20260729.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''    def _reconcile_positions(self, positions: list[Position]) -> None:\n        """Reconcile and reconstruct database portfolio lot states from broker positions."""\n        self.portfolio._lots.clear()\n        for p in positions:\n            strat = "manual"'''
new = '''    def _reconcile_positions(self, positions: list[Position]) -> None:\n        """Reconcile local lots to authoritative broker positions without erasing matching lot state."""\n        broker_symbols = {p.symbol for p in positions if abs(float(p.qty)) > 1e-9}\n        for local_symbol in list(self.portfolio._lots):\n            if local_symbol not in broker_symbols:\n                self.portfolio._lots.pop(local_symbol, None)\n                self._position_take_profits.pop(local_symbol, None)\n                self._position_peaks.pop(local_symbol, None)\n\n        for p in positions:\n            existing_lot = self.portfolio._lots.get(p.symbol)\n            if existing_lot is not None:\n                qty_matches = abs(float(existing_lot.qty) - float(p.qty)) <= 1e-9\n                price_scale = max(1.0, abs(float(p.avg_entry_price)))\n                avg_matches = abs(float(existing_lot.avg_price) - float(p.avg_entry_price)) <= 1e-8 * price_scale\n                if qty_matches and avg_matches:\n                    # Fill processing already established the same economic lot. Keep\n                    # its original/current stop, initial R, strategy and entry timestamp.\n                    p.strategy = existing_lot.strategy\n                    continue\n\n            strat = "manual"'''
if old not in src:
    raise SystemExit("reconciliation anchor not found; refusing unsafe patch")
src = src.replace(old, new, 1)

if src == original:
    raise SystemExit("no changes applied")
ENGINE.write_text(src, encoding="utf-8")
print("Applied reconciliation/fill ordering hardening to backend/app/core/engine.py")
