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


def test_alpaca_positions_reconcile_even_without_prior_failure():
    body = _tick_source()
    positions = body.index("positions = await self.broker.positions()")
    protection = body.index("await self.verify_broker_protective_stops(positions)", positions)
    block = body[positions:protection]
    assert "isinstance(self.broker, AlpacaBroker)" not in block
    assert "self._reconcile_positions(positions)" in block
    # Prior failure may affect logging, but must not gate reconciliation itself.
    assert 'if getattr(self, "reconciliation_failed", False):\n                    self._reconcile_positions' not in block


def test_rule41_runs_after_local_lot_repair():
    body = _tick_source()
    positions = body.index("positions = await self.broker.positions()")
    reconcile = body.index("self._reconcile_positions(positions)", positions)
    protection = body.index("await self.verify_broker_protective_stops(positions)", positions)
    assert reconcile < protection


def test_reconciliation_does_not_synthesize_fill_or_realized_trade():
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    def _reconcile_positions(")
    end = src.index("    async def verify_broker_protective_stops", start)
    body = src[start:end]
    assert ".on_fill(" not in body
    assert "Fill(" not in body
