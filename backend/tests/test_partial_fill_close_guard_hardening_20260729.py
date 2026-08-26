"""Regression contract for partial fills and close idempotency.

Updated for the 2026-08-25 hardening: the per-symbol close latch must be
releasable by fill-truth (requested close size fully executed) and by a
per-tick staleness reconciliation, not merely by resolving the parent order
object or waiting for another fill of the same symbol. See
_reconcile_pending_closes / _release_pending_close in app/core/engine.py.
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
    # All latch releases funnel through the audited helper.
    assert "self._release_pending_close(symbol" in body
    # Lot-accounting fallback still guards blind releases.
    assert "getattr(self.portfolio" in body and "_lots" in body and ".get(symbol)" in body


def _release_source() -> str:
    """Full engine source containing the latch lifecycle helpers."""
    return (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")


def test_release_helper_discards_latch_exactly_once():
    body = _release_source()
    helper_idx = body.index("def _release_pending_close")
    helper_body = body[helper_idx:]
    assert "self._pending_close_symbols.discard(symbol)" in helper_body
    assert "_pending_close_qtys.pop" in helper_body
    assert "_pending_close_submitted_at.pop" in helper_body


def test_close_guard_release_is_side_symmetric_for_long_exit_and_short_cover():
    body = _drain_source()
    apply_idx = body.index("self.portfolio.on_fill")
    release_idx = body.index("self._release_pending_close(symbol", apply_idx)
    release_context = body[apply_idx:release_idx + len("self._release_pending_close(symbol") + 100]
    assert "OrderSide.SELL" not in release_context
    assert "OrderSide.BUY" not in release_context


def test_partial_fill_keeps_close_guard_until_portfolio_accounting_finishes():
    body = _drain_source()
    apply_idx = body.index("self.portfolio.on_fill")
    lot_idx = body.index("getattr(self.portfolio", apply_idx)
    release_idx = body.index("self._release_pending_close(symbol", lot_idx)
    assert apply_idx < lot_idx < release_idx


def test_fill_truth_completion_releases_guard_after_partial_scale_out():
    """The exact 2026-08-24 wedge: residual runner lot + unresolvable close
    order used to hold the latch forever. Fill truth against the REQUESTED
    close size must release it."""
    body = _drain_source()
    assert "requested close size filled" in body
    assert "self._pending_close_qtys.get(symbol)" in body
    assert "cumulative_qty" in body


def test_stale_close_latch_is_force_released_every_tick():
    src = _release_source()
    recon_idx = src.index("async def _reconcile_pending_closes")
    recon = src[recon_idx:]
    assert "stale_after" in recon
    assert "_release_pending_close" in recon
    drain = _drain_source()
    # Reconciliation runs before any fill processing so a wedged latch cannot
    # swallow PIS exits even when no fills arrive.
    assert drain.index("_reconcile_pending_closes(now)") < drain.index("broker.drain_fills()")
