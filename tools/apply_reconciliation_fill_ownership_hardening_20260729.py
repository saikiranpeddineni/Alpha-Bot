"""Prevent broker-position reconciliation from double-owning live fill accounting.

_reconcile_positions is authoritative on startup/recovery, but clearing and rebuilding
all local lots while live broker fills may still be queued can make the subsequent fill
delta apply on top of an already broker-inclusive quantity.  During an in-process
recovery, preserve matching local lots and only reconstruct missing/mismatched symbols.
Startup remains a full reconstruction because the tracker is newly created.

Run from repository root:
    python tools/apply_reconciliation_fill_ownership_hardening_20260729.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''    def _reconcile_positions(self, positions: list[Position]) -> None:\n        """Reconcile and reconstruct database portfolio lot states from broker positions."""\n        self.portfolio._lots.clear()\n        for p in positions:\n'''
new = '''    def _reconcile_positions(self, positions: list[Position]) -> None:\n        """Reconcile local lots to broker positions without double-owning queued fills.\n\n        Startup uses a fresh PortfolioTracker and therefore naturally reconstructs every\n        broker position.  During live recovery, preserve a local lot only when its signed\n        quantity and average entry already match broker truth.  Missing/mismatched lots are\n        rebuilt from broker state, and broker-flat symbols are removed.  _tick drains broker\n        fills before calling this method, so a rebuilt lot cannot subsequently receive the\n        same already-observed fill delta in the normal recovery path.\n        """\n        broker_symbols = {p.symbol for p in positions if abs(float(p.qty)) > 1e-9}\n        for stale_symbol in list(self.portfolio._lots):\n            if stale_symbol not in broker_symbols:\n                self.portfolio._lots.pop(stale_symbol, None)\n\n        for p in positions:\n            existing_lot = self.portfolio._lots.get(p.symbol)\n            if existing_lot is not None:\n                qty_matches = abs(float(existing_lot.qty) - float(p.qty)) <= 1e-9\n                avg_matches = abs(float(existing_lot.avg_price) - float(p.avg_entry_price)) <= 1e-6\n                if qty_matches and avg_matches:\n                    # Preserve durable entry timestamp, strategy, initial risk and stop\n                    # metadata when broker quantity/average already agree.\n                    continue\n'''
if old not in src:
    raise SystemExit("reconciliation anchor not found; refusing unsafe patch")
src = src.replace(old, new, 1)

if src == original:
    raise SystemExit("no changes applied")
ENGINE.write_text(src, encoding="utf-8")
print("Applied reconciliation/fill ownership hardening to backend/app/core/engine.py")
