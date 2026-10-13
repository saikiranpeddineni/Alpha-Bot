"""Regression contract for broker-authoritative normal-cycle reconciliation.

Production source is materialized by:
    python tools/apply_tick_reconciliation_authority_hardening_20260729.py
"""
from pathlib import Path


def _tick_source() -> str:
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    async def _tick(self)")
    end = src.index("    async def _evaluate_symbol", start)
    return src[start:end]


def test_real_fills_are_drained_before_broker_position_authority_snapshot():
    body = _tick_source()
    # Use the second normal-cycle poll/drain block, immediately before account/positions.
    snapshot = body.index("account = await self.broker.account()")
    drain = body.rfind("await self._drain_fills(now)", 0, snapshot)
    positions = body.index("positions = await self.broker.positions()", snapshot)
    reconcile = body.index("self._reconcile_positions(positions)", positions)
    assert drain < snapshot < positions < reconcile
