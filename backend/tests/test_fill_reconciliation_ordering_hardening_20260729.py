"""Regression contract for fill/reconciliation causal ordering.

Production source is materialized by:
    python tools/apply_fill_reconciliation_ordering_hardening_20260729.py
"""
from pathlib import Path


def _engine_source() -> str:
    return (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")


def test_recovery_drains_fills_before_rebuilding_local_lots():
    src = _engine_source()
    anchor = src.index('if getattr(self, "reconciliation_failed", False):', src.index("async def _tick"))
    block = src[anchor:src.index("if positions and not getattr", anchor)]
    poll_idx = block.index("await self.broker.poll_fills()")
    drain_idx = block.index("await self._drain_fills(now)")
    refresh_idx = block.index("positions = await self.broker.positions()", drain_idx)
    rebuild_idx = block.index("self._reconcile_positions(positions)")
    assert poll_idx < drain_idx < refresh_idx < rebuild_idx


def test_reconcile_method_documents_drained_fill_precondition():
    src = _engine_source()
    start = src.index("    def _reconcile_positions(")
    body = src[start:src.index("    async def verify_broker_protective_stops", start)]
    assert "after the cycle's fill queue is drained" in body
    assert "Callers" in body and "draining fills first" in body


def test_normal_tick_still_drains_before_account_and_position_snapshot():
    src = _engine_source()
    tick = src[src.index("    async def _tick"):src.index("    async def _evaluate_symbol")]
    drain = tick.index("await self._drain_fills(now)")
    account = tick.index("account = await self.broker.account()", drain)
    positions = tick.index("positions = await self.broker.positions()", account)
    assert drain < account < positions
