"""Regression contract for broker-position/local-fill ownership.

Materialize with:
    python tools/apply_reconciliation_fill_ownership_hardening_20260729.py
"""
from pathlib import Path


def _source() -> str:
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    def _reconcile_positions")
    end = src.index("    async def verify_broker_protective_stops", start)
    return src[start:end]


def test_reconciliation_does_not_clear_all_live_lots_unconditionally():
    body = _source()
    assert "self.portfolio._lots.clear()" not in body
    assert "broker_symbols =" in body
    assert "stale_symbol not in broker_symbols" in body


def test_matching_live_lot_is_preserved_with_durable_trade_metadata():
    body = _source()
    assert "qty_matches" in body
    assert "avg_matches" in body
    assert "if qty_matches and avg_matches:" in body
    assert "Preserve durable entry timestamp, strategy, initial risk and stop" in body


def test_mismatched_broker_quantity_still_reconstructs_authoritatively():
    body = _source()
    match_idx = body.index("if qty_matches and avg_matches:")
    rebuild_idx = body.index("strat = \"manual\"", match_idx)
    assert match_idx < rebuild_idx
    assert "self.portfolio._lots[p.symbol] = _OpenLot(" in body


def test_tick_drains_real_fills_before_live_reconciliation_retry():
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    tick = src[src.index("    async def _tick"):src.index("    async def _evaluate_symbol")]
    first_drain = tick.index("await self._drain_fills(now)")
    retry = tick.index("self._reconcile_positions(positions)")
    assert first_drain < retry
