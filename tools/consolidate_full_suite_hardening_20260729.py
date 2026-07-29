"""Restore reviewed lifecycle hardening lost from the current production tree.

This is deliberately NOT the stale holistic-harvest applicator.  It composes only the
small, fail-closed/idempotent hardening steps whose production invariants were verified
against the current full-suite failures.  Run once from repository root, review the
diff, then execute the previously failing tests before any broader suite.
"""
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]

STEPS = [
    "apply_entry_exit_reconciliation_hardening_20260729.py",
    "apply_partial_fill_close_guard_hardening_20260729.py",
    "apply_bracket_child_protection_hardening_20260729.py",
    "apply_partial_bracket_protection_hardening_20260729.py",
    "apply_reconciliation_fill_ordering_hardening_20260729.py",
    "apply_tick_reconciliation_authority_hardening_20260729.py",
    "apply_durable_fill_notional_hardening_20260729.py",
    "apply_local_validation_failures_fix_20260729.py",
]

# Never invoke apply_holistic_live_exit_harvest_fix_20260729.py here.  That historical
# applicator predates later Rule-41/restart/fill hardening and previously overwrote it.
for name in STEPS:
    path = ROOT / "tools" / name
    if not path.exists():
        raise SystemExit(f"required reviewed hardening step missing: {name}")
    print(f"\n=== {name} ===")
    runpy.run_path(str(path), run_name="__main__")

print("\nConsolidated reviewed lifecycle hardening materialized. Do not commit until failed-case tests pass.")
