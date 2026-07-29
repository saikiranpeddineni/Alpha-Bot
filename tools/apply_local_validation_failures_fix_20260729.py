"""Fix defects exposed by the first local holistic validation run.

Repeat-safe: accepts the final production state after a previous validation run while
still failing closed if expected invariants are absent.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
VALIDATION_APPLIER = ROOT / "tools/apply_holistic_live_exit_validation_suite_20260729.py"

src = ENGINE.read_text(encoding="utf-8")
original = src
old = '''            from app.brokers.alpaca import AlpacaBroker\n            if isinstance(self.broker, AlpacaBroker):\n                if getattr(self, "reconciliation_failed", False):\n                    applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")\n                self._reconcile_positions(positions)\n\n            if positions and not getattr(self, "reconciliation_failed", False):\n'''
new = '''            # Reconciliation is a Broker-interface concern, not an Alpaca concrete-class\n            # concern.  Tests, hardened wrappers and future adapters must recover through\n            # the same authoritative positions() contract.\n            if getattr(self, "reconciliation_failed", False):\n                applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")\n            self._reconcile_positions(positions)\n\n            if positions and not getattr(self, "reconciliation_failed", False):\n'''
if old in src:
    src = src.replace(old, new, 1)
else:
    # Inspect only the tick reconciliation region. Other engine paths may legitimately
    # use AlpacaBroker concrete checks and must not make this applicator fail.
    pos_idx = src.find('positions = await self.broker.positions()')
    verify_idx = src.find('await self.verify_broker_protective_stops(positions)', pos_idx)
    if pos_idx < 0 or verify_idx < 0:
        raise SystemExit("tick reconciliation region missing")
    tick_region = src[pos_idx:verify_idx]
    if 'isinstance(self.broker, AlpacaBroker)' in tick_region:
        raise SystemExit("tick reconciliation concrete-class state is unexpected")
    if 'self._reconcile_positions(positions)' not in tick_region:
        raise SystemExit("Broker-interface reconciliation invariant missing")

old_geom = '''        # Stop must remain behind the completed close with a small geometry buffer.\n        prior_close = float(prior["close"])\n        px_buffer = max(0.01, prior_close * 0.0002)\n        desired_floor = min(desired_floor, prior_close - px_buffer) if side > 0 else max(desired_floor, prior_close + px_buffer)\n        if side > 0 and desired_floor <= entry:\n'''
new_geom = '''        # Stop geometry may constrain a *new* ratchet, but it must never lower an\n        # already broker-confirmed floor.  If the latest completed close has fallen\n        # through that floor, the broker stop is already authoritative; preserve the\n        # confirmed floor and do not manufacture a looser replacement.\n        prior_close = float(prior["close"])\n        px_buffer = max(0.01, prior_close * 0.0002)\n        geometry_floor = min(desired_floor, prior_close - px_buffer) if side > 0 else max(desired_floor, prior_close + px_buffer)\n        if current_floor is None:\n            desired_floor = geometry_floor\n        else:\n            desired_floor = max(geometry_floor, current_floor) if side > 0 else min(geometry_floor, current_floor)\n        if side > 0 and desired_floor <= entry:\n'''
if old_geom in src:
    src = src.replace(old_geom, new_geom, 1)
elif 'geometry_floor = min(desired_floor, prior_close - px_buffer)' not in src:
    raise SystemExit("runner monotonic geometry invariant missing")

if src != original:
    ENGINE.write_text(src, encoding="utf-8")

t = VALIDATION_APPLIER.read_text(encoding="utf-8")
t_original = t
old_test = '''    reconcile = src[src.index("async def _reconcile_positions"):src.index("async def _reconcile_orders")]\n'''
new_test = '''    reconcile = src[src.index("def _reconcile_positions"):src.index("async def verify_broker_protective_stops")]\n'''
if old_test in t:
    t = t.replace(old_test, new_test, 1)
elif new_test not in t:
    raise SystemExit("restart regression signature state unexpected")
if t != t_original:
    VALIDATION_APPLIER.write_text(t, encoding="utf-8")

print("Local validation failure fixes already applied" if src == original and t == t_original else "Applied local validation failure fixes")
