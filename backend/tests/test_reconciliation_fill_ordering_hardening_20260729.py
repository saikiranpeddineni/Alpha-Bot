"""Regression contract for fill-drain -> broker-position reconciliation ordering."""
from pathlib import Path


def _tick_source() -> str:
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    async def _tick(")
    end = src.index("    async def _evaluate_symbol", start)
    return src[start:end]


def test_tick_drains_fill_deltas_before_authoritative_position_fetch():
    body = _tick_source()
    drain = body.index("await self._drain_fills(now)")
    positions = body.index("positions = await self.broker.positions()", drain)
    assert drain < positions
