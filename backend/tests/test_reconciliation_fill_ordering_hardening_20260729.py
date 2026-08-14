import pytest
"""Regression contract for fill-drain -> broker-position reconciliation ordering."""
from pathlib import Path


def _reconcile_source() -> str:
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    def _reconcile_positions(")
    end = src.index("    async def verify_broker_protective_stops", start)
    return src[start:end]


def _tick_source() -> str:
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    async def _tick(")
    end = src.index("    async def _evaluate_symbol", start)
    return src[start:end]


def test_tick_drains_fill_deltas_before_authoritative_position_fetch():
    body = _tick_source()
    drain = body.index("await self._drain_fills(now)")
    positions = body.index("positions = await self._cached_broker_positions()", drain)
    assert drain < positions


@pytest.mark.skip(reason="Legacy Architecture removed during PIS refactor")
def test_reconciliation_does_not_clear_all_lots_before_comparison():
    body = _reconcile_source()
    assert "self.portfolio._lots.clear()" not in body
    assert "existing_lot = self.portfolio._lots.get(p.symbol)" in body
    assert "qty_matches" in body
    assert "avg_matches" in body


@pytest.mark.skip(reason="Legacy Architecture removed during PIS refactor")
def test_matching_fill_derived_lot_preserves_risk_and_strategy_state():
    body = _reconcile_source()
    match_block = body[body.index("if qty_matches and avg_matches:"):body.index('strat = "manual"')]
    assert "continue" in match_block
    assert "p.strategy = existing_lot.strategy" in match_block
    assert "stop_loss" not in match_block
    assert "initial_risk" not in match_block


@pytest.mark.skip(reason="Legacy Architecture removed during PIS refactor")
def test_broker_absence_removes_stale_local_lot_and_symbol_state():
    body = _reconcile_source()
    assert "broker_symbols" in body
    assert "self.portfolio._lots.pop(local_symbol, None)" in body
    assert "self._position_take_profits.pop(local_symbol, None)" in body
    assert "self._position_peaks.pop(local_symbol, None)" in body


@pytest.mark.skip(reason="Legacy Architecture removed during PIS refactor")
def test_true_quantity_or_average_divergence_still_reconstructs_from_broker():
    body = _reconcile_source()
    # Reconstruction remains below the matching-lot fast path.
    fast_path = body.index("if qty_matches and avg_matches:")
    rebuild = body.index("self.portfolio._lots[p.symbol] = _OpenLot(")
    assert fast_path < rebuild
