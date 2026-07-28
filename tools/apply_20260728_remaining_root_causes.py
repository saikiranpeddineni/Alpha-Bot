"""Close the remaining verified 2026-07-28 runtime root causes.

Idempotent and surgical: no strategy, ML threshold, sizing, or exit-policy tuning.
"""
from __future__ import annotations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALPACA = ROOT / "backend/app/brokers/alpaca.py"
RISK = ROOT / "backend/app/risk/manager.py"


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    src = path.read_text(encoding="utf-8")
    if new in src:
        print(f"already applied: {label}")
        return
    if old not in src:
        raise RuntimeError(f"anchor not found for {label}: {path}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    print(f"applied: {label}")


# RC-05: 40310000 is overloaded by Alpaca. The INTC/NOW payload explicitly said
# existing_qty == held_for_orders and available == 0: an exit-order reservation,
# not a shorting-permission denial. Never poison account/symbol shortability from
# a numeric code alone; only explicit shortability semantics may do that.
replace_once(
    ALPACA,
    '''                err_msg = str(exc).lower()\n                if "not allowed to short" in err_msg or "40310000" in err_msg:\n                    self._shorting_enabled = False\n                    self._shortable_cache[req.symbol.upper()] = False\n''',
    '''                err_msg = str(exc).lower()\n                short_permission_denied = (\n                    "not allowed to short" in err_msg\n                    or "shorting is not permitted" in err_msg\n                    or "asset is not shortable" in err_msg\n                    or "not shortable" in err_msg\n                )\n                if short_permission_denied:\n                    self._shorting_enabled = False\n                    self._shortable_cache[req.symbol.upper()] = False\n''',
    "classify 40310000 by semantics, not numeric code",
)

# RC-06: the actual NOK 42210000 originates one layer earlier than the adapter.
# RiskManager validates raw floating-point target vs raw signal price, then rounds
# the child price to cents. Example: a short target can be < raw price yet round
# to the same cent as the broker base. Validate the executable cent prices and
# enforce Alpaca's one-cent directional separation BEFORE constructing the order.
replace_once(
    RISK,
    '''        order = OrderRequest(\n            symbol=symbol,\n            side=side,\n            qty=qty,\n            order_type=OrderType.MARKET,\n            stop_loss=round(stop_loss, 2),\n            take_profit=round(take_profit, 2),\n            strategy=signal.strategy,\n            reason=signal.reason,\n        )\n''',
    '''        executable_entry = round(float(price), 2)\n        executable_stop = round(float(stop_loss), 2)\n        executable_target = round(float(take_profit), 2)\n        if side == OrderSide.BUY:\n            if executable_stop >= executable_entry:\n                executable_stop = round(executable_entry - 0.01, 2)\n            if executable_target <= executable_entry:\n                executable_target = round(executable_entry + 0.01, 2)\n        else:\n            if executable_stop <= executable_entry:\n                executable_stop = round(executable_entry + 0.01, 2)\n            if executable_target >= executable_entry:\n                executable_target = round(executable_entry - 0.01, 2)\n        if executable_stop <= 0 or executable_target <= 0:\n            return RiskDecision(False, "broker-valid bracket geometry unavailable at current price")\n\n        order = OrderRequest(\n            symbol=symbol,\n            side=side,\n            qty=qty,\n            order_type=OrderType.MARKET,\n            stop_loss=executable_stop,\n            take_profit=executable_target,\n            strategy=signal.strategy,\n            reason=signal.reason,\n        )\n''',
    "validate bracket geometry after executable cent rounding",
)

for path in (ALPACA, RISK):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print("Remaining July-28 root-cause fixes applied; syntax compile passed.")
