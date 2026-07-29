"""One-command materialize + validation runner for holistic live exits.

Usage from repo root:
    python tools/run_holistic_live_exit_validation_20260729.py

It applies the production lifecycle patch, installs the full behavioral matrix,
then runs the focused exit/protection/reconciliation/EOD regression suites.
"""
from __future__ import annotations

from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / "tools/apply_holistic_live_exit_validation_suite_20260729.py"), run_name="__main__")

backend = ROOT / "backend"
tests = [
    "tests/test_holistic_live_exit_harvest.py",
    "tests/test_exit_lifecycle_strategy_ownership_20260727.py",
    "tests/test_eod_liquidation_authority.py",
    "tests/test_hard_halt_liquidation_authority.py",
    "tests/test_008_fail_closed_reconciliation.py",
    "tests/test_011_ambiguous_submit_failures.py",
]
cmd = [sys.executable, "-m", "pytest", "-q", *tests]
print("Running:", " ".join(cmd))
raise SystemExit(subprocess.call(cmd, cwd=backend))
