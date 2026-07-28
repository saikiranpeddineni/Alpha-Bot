"""Preserve entry-bound risk metadata across durable portfolio checkpoints.

Surgical correctness fix: no strategy, sizing, threshold, exit-policy, or architecture changes.
Without these fields a restart can lose the original stop/risk-dollar basis, corrupting
R-multiple/MFE analytics and recovery-stop provenance for an already-open lot.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACKER = ROOT / "backend/app/portfolio/tracker.py"
text = TRACKER.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global text
    if new in text:
        print(f"already applied: {label}")
        return
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    text = text.replace(old, new, 1)
    print(f"applied: {label}")


replace_once(
    '''                    "stop_loss": lot.stop_loss,\n                    "initial_risk_per_share": lot.initial_risk_per_share,\n                    "commission_basis": lot.commission_basis,\n''',
    '''                    "stop_loss": lot.stop_loss,\n                    "initial_stop_loss": lot.initial_stop_loss,\n                    "initial_risk_per_share": lot.initial_risk_per_share,\n                    "initial_risk_dollars": lot.initial_risk_dollars,\n                    "commission_basis": lot.commission_basis,\n''',
    "checkpoint entry-risk metadata",
)

replace_once(
    '''                stop_loss=lot.get("stop_loss"),\n                initial_risk_per_share=lot.get("initial_risk_per_share"),\n                commission_basis=float(lot.get("commission_basis", 0.0)),\n''',
    '''                stop_loss=lot.get("stop_loss"),\n                initial_stop_loss=lot.get("initial_stop_loss", lot.get("stop_loss")),\n                initial_risk_per_share=lot.get("initial_risk_per_share"),\n                initial_risk_dollars=float(lot.get("initial_risk_dollars", 0.0)),\n                commission_basis=float(lot.get("commission_basis", 0.0)),\n''',
    "restore entry-risk metadata",
)

TRACKER.write_text(text, encoding="utf-8")
compile(text, str(TRACKER), "exec")
print("Portfolio restart-fidelity fix applied; syntax compile passed.")
