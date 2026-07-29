"""Harden Rule-41 protection for partially-filled Alpaca bracket entries.

Alpaca activates bracket exit legs only after the entry order is completely
filled.  Therefore a partially-filled bracket creates a real position while its
stop/take-profit children are not yet authoritative protection.

Rule-41 must not submit a competing standalone stop while the bracket parent is
still capable of filling more shares.  Instead, freeze exposure by cancelling
the remaining bracket entry, wait until the broker no longer reports that parent
as open, then recover protection for the authoritative position quantity.

Run from repository root:
    python tools/apply_partial_bracket_protection_hardening_20260729.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''                state = self._protection_state.get(p.symbol)\n                side = OrderSide.SELL if p.qty > 0 else OrderSide.BUY\n                required_qty = abs(p.qty)\n\n                symbol_exit_orders = ['''
new = '''                state = self._protection_state.get(p.symbol)\n                side = OrderSide.SELL if p.qty > 0 else OrderSide.BUY\n                required_qty = abs(p.qty)\n\n                # Alpaca bracket exits are not active until the entry parent is\n                # completely filled.  A partially-filled bracket therefore leaves\n                # a real position temporarily unprotected.  Freeze exposure first:\n                # cancel the still-fillable entry parent, wait for broker confirmation,\n                # then recover a standalone stop sized to the authoritative position.\n                entry_side = OrderSide.BUY if p.qty > 0 else OrderSide.SELL\n                partial_bracket_entries = [\n                    o for o in open_orders\n                    if o.symbol == p.symbol\n                    and o.side == entry_side\n                    and str(getattr(o, "order_class", "") or "").lower() == "bracket"\n                    and str(getattr(o, "leg", "") or "").lower() == "entry"\n                    and o.status == OrderStatus.PARTIALLY_FILLED\n                    and float(getattr(o, "filled_qty", 0.0) or 0.0) > 0\n                    and float(getattr(o, "remaining_qty", 0.0) or 0.0) > 1e-9\n                ]\n                if partial_bracket_entries:\n                    if state == ProtectionState.AWAITING_CANCEL_CONFIRMATION:\n                        applog.info(\n                            "engine.protection",\n                            f"#41: Waiting for partial bracket entry cancellation confirmation for {p.symbol}; "\n                            f"{len(partial_bracket_entries)} parent order(s) still open.",\n                        )\n                        continue\n                    cancel_failed = False\n                    for parent in partial_bracket_entries:\n                        try:\n                            c_res = self.broker.cancel(parent.id)\n                            if inspect.isawaitable(c_res):\n                                await c_res\n                            applog.warning(\n                                "engine.protection",\n                                f"#41: Partial bracket entry {parent.id} for {p.symbol} has "\n                                f"filled={parent.filled_qty}/{parent.qty}; requested cancellation of remaining "\n                                "entry quantity before protective-stop recovery.",\n                            )\n                        except Exception as cancel_exc:\n                            cancel_failed = True\n                            applog.error(\n                                "engine.protection",\n                                f"#41: Failed to cancel partial bracket entry {parent.id} for {p.symbol}: {cancel_exc}",\n                            )\n                    if not cancel_failed:\n                        self._protection_state[p.symbol] = ProtectionState.AWAITING_CANCEL_CONFIRMATION\n                    continue\n\n                symbol_exit_orders = ['''
if old not in src:
    raise SystemExit("Rule-41 unprotected-position anchor not found; refusing unsafe patch")
src = src.replace(old, new, 1)

if src == original:
    raise SystemExit("no changes applied")
ENGINE.write_text(src, encoding="utf-8")
print("Applied partial-bracket protection hardening to backend/app/core/engine.py")
