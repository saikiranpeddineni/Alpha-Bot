"""Regression contract for Rule-41 handling of partial bracket entries.

Production source is materialized by:
    python tools/apply_partial_bracket_protection_hardening_20260729.py
"""
from pathlib import Path


def _protection_source() -> str:
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    async def verify_broker_protective_stops(", src.index("    async def verify_broker_protective_stops(") + 1)
    end = src.index("    async def _reconcile_pending_submissions", start)
    return src[start:end]


def test_partial_bracket_parent_is_detected_before_recovery_stop_submission():
    body = _protection_source()
    detect = body.index("partial_bracket_entries = [")
    submit = body.index("# Pre-submission geometry validation")
    assert detect < submit
    assert 'o.status == OrderStatus.PARTIALLY_FILLED' in body
    assert 'float(getattr(o, "remaining_qty", 0.0) or 0.0) > 1e-9' in body


def test_partial_bracket_parent_freezes_exposure_by_canceling_parent():
    body = _protection_source()
    block = body[body.index("if partial_bracket_entries:"):body.index("symbol_exit_orders = [", body.index("if partial_bracket_entries:"))]
    assert "self.broker.cancel(parent.id)" in block
    assert "ProtectionState.AWAITING_CANCEL_CONFIRMATION" in block
    assert "continue" in block
    assert "self.broker.submit" not in block


def test_partial_bracket_cancel_confirmation_is_idempotent():
    body = _protection_source()
    block = body[body.index("if partial_bracket_entries:"):body.index("symbol_exit_orders = [", body.index("if partial_bracket_entries:"))]
    assert "state == ProtectionState.AWAITING_CANCEL_CONFIRMATION" in block
    assert "still open" in block


def test_detection_is_directionally_symmetric_for_long_and_short_entries():
    body = _protection_source()
    assert "entry_side = OrderSide.BUY if p.qty > 0 else OrderSide.SELL" in body
    assert "and o.side == entry_side" in body
