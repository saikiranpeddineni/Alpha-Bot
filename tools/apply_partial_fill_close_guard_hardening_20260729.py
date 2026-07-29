"""Harden partial-fill state across entry -> exit lifecycle.

Fixes two coupled production defects in TradingEngine._drain_fills:
1. An incremental partial fill must not mark the whole order FILLED or replace
   filled_qty with only the latest delta.
2. A close guard must survive partial exits and work symmetrically for long SELL
   closes and short BUY covers.  It is released only when the local lot is flat
   (or has flipped, which is terminal for the original close lifecycle).

Run from repository root:
    python tools/apply_partial_fill_close_guard_hardening_20260729.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''            if order is not None and order.status != OrderStatus.FILLED:\n                # Paper orders share objects and are already updated; Alpaca\n                # orders are snapshots that need their fill state applied.\n                order.status = OrderStatus.FILLED\n                order.filled_qty = fill.qty\n                order.filled_avg_price = fill.price\n                order.filled_at = fill.ts\n            \n            symbol = fill.symbol.upper()\n            # Release pending-close guard when sell fill arrives\n            if fill.side == OrderSide.SELL:\n                self._pending_close_symbols.discard(symbol)\n            peak = self._position_peaks.get(symbol)'''
new = '''            if order is not None:\n                # Broker fills are incremental deltas.  Preserve cumulative order\n                # truth: a partial execution must never make the parent look FILLED\n                # or replace filled_qty with only the newest delta.\n                previous_filled = max(0.0, float(getattr(order, "filled_qty", 0.0) or 0.0))\n                broker_cumulative = float(getattr(fill, "cumulative_qty", 0.0) or 0.0)\n                if broker_cumulative > 0:\n                    cumulative_filled = max(previous_filled, broker_cumulative)\n                else:\n                    cumulative_filled = previous_filled + max(0.0, float(fill.qty))\n                order_qty = max(0.0, float(getattr(order, "qty", 0.0) or 0.0))\n                order.filled_qty = cumulative_filled\n                order.filled_avg_price = fill.price\n                if order_qty > 0 and cumulative_filled + 1e-9 >= order_qty:\n                    order.status = OrderStatus.FILLED\n                    order.filled_at = fill.ts\n                else:\n                    order.status = OrderStatus.PARTIALLY_FILLED\n                    order.filled_at = None\n\n            symbol = fill.symbol.upper()\n            peak = self._position_peaks.get(symbol)'''
if old not in src:
    raise SystemExit("partial-fill order-state anchor not found; refusing unsafe patch")
src = src.replace(old, new, 1)

old = '''            ack_fill = getattr(self.broker, "ack_fill_processed", None)\n            if ack_fill is not None:\n                ack_fill(fill)\n            if trade is not None:'''
new = '''            ack_fill = getattr(self.broker, "ack_fill_processed", None)\n            if ack_fill is not None:\n                ack_fill(fill)\n\n            # Close/cover idempotency is position-lifecycle based, not side based.\n            # Keep the guard through partial SELL exits and partial BUY covers; only\n            # release it after portfolio accounting proves the original lot is gone.\n            if symbol in self._pending_close_symbols:\n                remaining_lot = self.portfolio._lots.get(symbol)\n                if remaining_lot is None or abs(float(remaining_lot.qty)) < 1e-9:\n                    self._pending_close_symbols.discard(symbol)\n\n            if trade is not None:'''
if old not in src:
    raise SystemExit("close-guard release anchor not found")
src = src.replace(old, new, 1)

if src == original:
    raise SystemExit("no changes applied")
ENGINE.write_text(src, encoding="utf-8")
print("Applied partial-fill/order/close-guard hardening to backend/app/core/engine.py")
