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
    assert 'getattr(fill, "cumulative_qty"' in body
    assert "OrderStatus.PARTIALLY_FILLED" in body
    assert "order.filled_qty" in body
    assert "order.filled_qty = fill.qty" not in body


def test_close_guard_is_not_released_merely_because_a_sell_fill_arrived():
    body = _drain_source()
    assert "if fill.side == OrderSide.SELL" not in body
    assert "self.portfolio.on_fill" in body
    assert "_pending_close_symbols.discard" in body
    assert "getattr(self.portfolio" in body and "_lots" in body and ".get(symbol)" in body


def test_close_guard_release_is_side_symmetric_for_long_exit_and_short_cover():
    body = _drain_source()
    apply_idx = body.index("self.portfolio.on_fill")
    release_idx = body.index("_pending_close_symbols.discard", apply_idx)
    release_context = body[apply_idx:release_idx + len("_pending_close_symbols.discard") + 100]
    assert "OrderSide.SELL" not in release_context
    assert "OrderSide.BUY" not in release_context


def test_partial_fill_keeps_close_guard_until_portfolio_accounting_finishes():
    body = _drain_source()
    apply_idx = body.index("self.portfolio.on_fill")
    lot_idx = body.index("getattr(self.portfolio", apply_idx)
    release_idx = body.index("_pending_close_symbols.discard", lot_idx)
    assert apply_idx < lot_idx < release_idx
