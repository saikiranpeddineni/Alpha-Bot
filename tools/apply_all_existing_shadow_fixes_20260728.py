from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Apply the reviewed surgical patch set.
runpy.run_path(str(ROOT / "tools/apply_research_evidence_integrity_fix_20260728.py"), run_name="__main__")

# Keep SQLite's existing durability level. WAL + busy_timeout address contention;
# changing synchronous durability is unnecessary for a research fix and could alter
# production persistence guarantees.
db_path = ROOT / "backend/app/db/database.py"
src = db_path.read_text(encoding="utf-8")
src = src.replace('            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")\n', "")
db_path.write_text(src, encoding="utf-8")

# Restore only the EXISTING frozen Target-D artifact when its original frozen
# training source is available. Never substitute newer data.
model = ROOT / "backend/app/ai/models/target_d_shadow_challenger.joblib"
source = ROOT / "scratch/ml_alignment_research/strategy_candidate_events.parquet"
builder = ROOT / "scripts/build_target_d_shadow_artifact.py"
if not model.exists() and source.exists() and builder.exists():
    print("Building missing Target-D artifact from the existing frozen source...")
    subprocess.run([sys.executable, str(builder)], cwd=ROOT, check=True)
elif not model.exists():
    print("Target-D artifact remains unavailable: original frozen source is not present. No replacement model was created.")

print("ONE_SHOT_SHADOW_FIX=APPLIED")
print("Production strategy/risk/sizing/broker decisions were not modified by this patch runner.")
