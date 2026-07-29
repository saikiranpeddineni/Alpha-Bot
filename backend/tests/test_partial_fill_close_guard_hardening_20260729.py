"""Regression contract for partial fills and close idempotency.

Production source is materialized by:
    python tools/apply_partial_fill_close_guard_hardening_20260729.py
"""
from pathlib import Path


def _drain_source() -> str:
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    async def _drain_fills")
    end = src.index("    # ------------------------------------------------------------------\n    # broadcast payloads", start)
    return src[start:end]


def test_incremental_fill_does_not_unconditionally_mark_order_filled():
    body = _drain_source()
    assert "cumulative_filled" in body
    assert "OrderStatus.PARTIALLY_FILLED" in body
    assert "cumulative_filled + 1e-9 >= order_qty" in body
    assert "order.filled_qty = fill.qty" not in body


def test_close_guard_is_not_released_merely_because_a_sell_fill_arrived():
    body = _drain_source()
    assert "if fill.side == OrderSide.SELL" not in body
    assert "remaining_lot = self.portfolio._lots.get(symbol)" in body
    assert "abs(float(remaining_lot.qty)) < 1e-9" in body


def test_close_guard_release_is_side_symmetric_for_long_exit_and_short_cover():
    body = _drain_source()
    # The release decision must be based on surviving lot state, not BUY/SELL side.
    release = body[body.index("# Close/cover idempotency"):body.index("if trade is not None:", body.index("# Close/cover idempotency"))]
    assert "OrderSide.SELL" not in release
    assert "OrderSide.BUY" not in release
    assert "_pending_close_symbols.discard(symbol)" in release


def test_partial_fill_keeps_close_guard_until_portfolio_accounting_finishes():
    body = _drain_source()
    apply_idx = body.index("trade = self.portfolio.on_fill")
    release_idx = body.index("remaining_lot = self.portfolio._lots.get(symbol)")
    assert apply_idx < release_idx
