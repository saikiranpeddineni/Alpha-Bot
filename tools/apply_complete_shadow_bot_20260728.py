from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Order matters: first correct the evidence semantics/durability of the existing
# shadow components, then put the ShadowAlphaBot lifecycle facade around them.
runpy.run_path(str(ROOT / "tools/apply_all_existing_shadow_fixes_20260728.py"), run_name="__main__")
runpy.run_path(str(ROOT / "tools/apply_shadow_bot_runtime_20260728.py"), run_name="__main__")

print("COMPLETE_SHADOW_BOT=APPLIED")
print("Existing components only: Spawn, Target-D, outcome joiner, exit challengers/Harvest evidence, canonical shadow bus.")
print("No new models, model training, strategies, seeds, production gates, broker authority, or auto-promotion.")
