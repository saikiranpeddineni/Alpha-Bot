"""Harden Rule-41 bracket parent/child protection classification.

A broker bracket entry parent can expose child stop/TP prices in its nested legs.
AlpacaBroker._to_order intentionally harvests those prices for metadata, but that
must not turn the entry parent itself into proof of an active protective stop.
Only a true STOP/STOP_LIMIT order or an explicit stop child may satisfy Rule-41.

Run from repository root:
    python tools/apply_bracket_child_protection_hardening_20260729.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")

old = '''        is_stop_leg = (\n            (\n                getattr(order, "leg", "") in {"stop", "stop_loss"}\n                or getattr(order, "order_class", "") in {"bracket", "oco", "oto"}\n                or bool(getattr(order, "parent_id", None))\n            )\n            and order.stop_price is not None\n        )\n\n        if not (is_standalone_stop or is_stop_leg):\n            return False\n'''
new = '''        # A bracket/OTO/OCO *parent* may carry a child stop_price copied from\n        # nested broker legs for display/reconciliation metadata.  Parent class or\n        # parent_id alone is therefore not proof that this order is the stop leg.\n        # Rule-41 accepts only an explicit stop child or a standalone STOP order.\n        leg = str(getattr(order, "leg", "") or "").lower()\n        is_stop_leg = (\n            leg in {"stop", "stop_loss"}\n            and order.stop_price is not None\n        )\n\n        if not (is_standalone_stop or is_stop_leg):\n            return False\n'''
if old not in src:
    raise SystemExit("Rule-41 stop-leg classification anchor not found; refusing unsafe patch")
src = src.replace(old, new, 1)
ENGINE.write_text(src, encoding="utf-8")
print("Applied bracket child protection hardening to backend/app/core/engine.py")
