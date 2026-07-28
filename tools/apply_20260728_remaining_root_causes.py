"""Close the remaining verified 2026-07-28 runtime root causes.

Idempotent source transformation. This intentionally does not tune strategies,
ML thresholds, sizing, or exit policy.

RC-05: Alpaca 40310000 was treated as a shorting-permission failure even when
       the broker explicitly reported shares held by an existing exit order.
       That poisoned the account-wide shorting capability cache.
RC-06: Bracket child prices were rounded independently, allowing a valid
       pre-rounding bracket to collapse onto the same cent at low prices.
       Alpaca then rejected the bracket (42210000).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALPACA = ROOT / "backend/app/brokers/alpaca.py"


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    src = path.read_text(encoding="utf-8")
    if new in src:
        print(f"already applied: {label}")
        return
    if old not in src:
        raise RuntimeError(f"anchor not found for {label}: {path}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    print(f"applied: {label}")


# Keep bracket geometry valid after cent rounding.  This is deliberately based
# only on the relationship between stop and target: for a long bracket target is
# below the stop only if geometry is inverted; for a short bracket target is
# above the stop only if geometry is inverted.  We do NOT invent an entry price.
# The minimum-cent separation prevents rounding from making the two children
# degenerate while preserving the strategy's requested side and risk direction.
replace_once(
    ALPACA,
    '''        if req.stop_loss is not None and req.take_profit is not None:\n            from alpaca.trading.enums import OrderClass\n\n            kwargs["order_class"] = OrderClass.BRACKET\n            kwargs["stop_loss"] = StopLossRequest(stop_price=round(req.stop_loss, 2))\n            kwargs["take_profit"] = TakeProfitRequest(limit_price=round(req.take_profit, 2))\n''',
    '''        if req.stop_loss is not None and req.take_profit is not None:\n            from alpaca.trading.enums import OrderClass\n\n            stop_px = round(float(req.stop_loss), 2)\n            target_px = round(float(req.take_profit), 2)\n            if stop_px <= 0 or target_px <= 0:\n                raise ValueError("Bracket stop/target must be positive")\n            # Long: stop < target. Short: target < stop. The strategy/risk layer\n            # already chooses the absolute levels; the adapter only guarantees\n            # broker-valid cent geometry after rounding.\n            if req.side == OrderSide.BUY and target_px <= stop_px:\n                target_px = round(stop_px + 0.01, 2)\n            elif req.side == OrderSide.SELL and target_px >= stop_px:\n                target_px = round(max(0.01, stop_px - 0.01), 2)\n            kwargs["order_class"] = OrderClass.BRACKET\n            kwargs["stop_loss"] = StopLossRequest(stop_price=stop_px)\n            kwargs["take_profit"] = TakeProfitRequest(limit_price=target_px)\n''',
    "cent-safe bracket child geometry",
)

# 40310000 is not a shorting capability code. Alpaca also emits it when a long
# position's shares are already held by another sell order (the exact INTC/NOW
# incident). Only explicit shorting-permission language may poison the shorting
# capability/cache. The rejected order is still returned normally.
replace_once(
    ALPACA,
    '''                err_msg = str(exc).lower()\n                if "not allowed to short" in err_msg or "40310000" in err_msg:\n                    self._shorting_enabled = False\n                    self._shortable_cache[req.symbol.upper()] = False\n''',
    '''                err_msg = str(exc).lower()\n                short_permission_denied = (\n                    "not allowed to short" in err_msg\n                    or "shorting is not permitted" in err_msg\n                    or "asset is not shortable" in err_msg\n                    or "not shortable" in err_msg\n                )\n                if short_permission_denied:\n                    self._shorting_enabled = False\n                    self._shortable_cache[req.symbol.upper()] = False\n''',
    "do not poison shorting state on held-share 40310000",
)

compile(ALPACA.read_text(encoding="utf-8"), str(ALPACA), "exec")
print("Remaining July-28 root-cause fixes applied; syntax compile passed.")
