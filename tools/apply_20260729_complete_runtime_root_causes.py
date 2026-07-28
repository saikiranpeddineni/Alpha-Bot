"""Apply the complete verified heartbeat/runtime root-cause hardening set.

This runner is intentionally deterministic and ordered. It does not tune strategy
signals, ML/meta thresholds, position sizing, scanner economics, stops, or targets.
It only applies verified runtime-safety, lifecycle, reconciliation, research
isolation, observability, restart-fidelity, and regression-test corrections.
"""
from __future__ import annotations

from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]

SCRIPTS = [
    # July-28 verified broker/risk/runtime defects.
    "tools/apply_heartbeat_runtime_root_causes_20260728.py",
    "tools/apply_20260728_remaining_root_causes.py",
    "tools/apply_20260728_heartbeat_state_machine_completion.py",
    # July-29 architecture/lifecycle pass discovered during the deeper audit.
    "tools/apply_20260729_architecture_runtime_fixes.py",
    "tools/apply_20260729_lifecycle_isolation_fixes.py",
    "tools/apply_20260729_submission_reconciliation_fixes.py",
    "tools/apply_20260729_shadow_observability_fixes.py",
    "tools/apply_20260729_forensic_report_corrections.py",
    "tools/apply_20260729_portfolio_restart_fidelity_fix.py",
    # Regression coverage is applied last, after production anchors converge.
    "tools/apply_20260728_architecture_audit_tests.py",
    "tools/apply_20260729_architecture_audit_tests.py",
    "tools/apply_20260729_lifecycle_isolation_tests.py",
    "tools/apply_20260729_portfolio_restart_fidelity_test.py",
]

for rel in SCRIPTS:
    path = ROOT / rel
    if not path.exists():
        raise FileNotFoundError(rel)
    print(f"==> {rel}")
    runpy.run_path(str(path), run_name="__main__")

print("Complete heartbeat/runtime root-cause hardening and regression coverage applied.")
