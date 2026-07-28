"""Apply every 2026-07-29 runtime architecture fix in dependency order.

This is intentionally a thin deterministic runner so the branch cannot be left
half-patched by running only one audit applicator.
"""
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    "tools/apply_20260729_architecture_runtime_fixes.py",
    "tools/apply_20260729_lifecycle_isolation_fixes.py",
    "tools/apply_20260729_submission_reconciliation_fixes.py",
    "tools/apply_20260729_architecture_audit_tests.py",
    "tools/apply_20260729_lifecycle_isolation_tests.py",
]

for rel in SCRIPTS:
    path = ROOT / rel
    if not path.exists():
        raise FileNotFoundError(rel)
    print(f"==> {rel}")
    runpy.run_path(str(path), run_name="__main__")

print("All 2026-07-29 runtime fixes and regression-test additions applied.")
