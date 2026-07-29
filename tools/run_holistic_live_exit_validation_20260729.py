"""One-command materialize + focused validation runner for the live trade lifecycle.

Usage from repo root:
    python tools/run_holistic_live_exit_validation_20260729.py

This materializes the surgical production fixes, including defects exposed by local
validation, then runs the focused lifecycle regression suites. The final gate remains
the entire local pytest suite.
"""
from __future__ import annotations

from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Materialize production fixes in dependency order.
runpy.run_path(str(ROOT / "tools/apply_holistic_live_exit_validation_suite_20260729.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_entry_exit_reconciliation_hardening_20260729.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_partial_fill_close_guard_hardening_20260729.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_bracket_child_protection_hardening_20260729.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_partial_bracket_protection_hardening_20260729.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_reconciliation_fill_ordering_hardening_20260729.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_tick_reconciliation_authority_hardening_20260729.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_durable_fill_notional_hardening_20260729.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_local_validation_failures_fix_20260729.py"), run_name="__main__")

backend = ROOT / "backend"
tests = [
    "tests/test_production_integrity.py",
    "tests/test_same_cycle_entry_reservations.py",
    "tests/test_011_ambiguous_submit_failures.py",
    "tests/test_entry_exit_reconciliation_hardening_20260729.py",
    "tests/test_008_fail_closed_reconciliation.py",
    "tests/test_issue_016_reconciliation_fail_closed.py",
    "tests/test_alpaca_hardened_restart_reconciliation.py",
    "tests/test_restart_fill_reconciliation.py",
    "tests/test_fill_crash_boundary_atomicity_20260729.py",
    "tests/test_alpaca_incremental_fills.py",
    "tests/test_durable_fill_notional_restart_20260729.py",
    "tests/test_partial_fill_close_guard_hardening_20260729.py",
    "tests/test_reconciliation_fill_ordering_hardening_20260729.py",
    "tests/test_tick_reconciliation_authority_hardening_20260729.py",
    "tests/test_rule_41_protection.py",
    "tests/test_protection_and_accounting_refactor.py",
    "tests/test_bracket_child_protection_hardening_20260729.py",
    "tests/test_partial_bracket_protection_hardening_20260729.py",
    "tests/test_holistic_live_exit_harvest.py",
    "tests/test_exit_lifecycle_strategy_ownership_20260727.py",
    "tests/test_045_duplicate_close_guard.py",
    "tests/test_eod_liquidation_authority.py",
    "tests/test_hard_halt_liquidation_authority.py",
]

missing = [test for test in tests if not (backend / test).exists()]
if missing:
    print("Focused lifecycle runner references missing tests:")
    for path in missing:
        print(" -", path)
    raise SystemExit(2)

cmd = [sys.executable, "-m", "pytest", "-q", *tests]
print("Running:", " ".join(cmd))
raise SystemExit(subprocess.call(cmd, cwd=backend))
