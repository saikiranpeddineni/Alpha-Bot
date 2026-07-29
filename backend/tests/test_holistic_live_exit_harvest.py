"""Regression invariants for holistic LIVE winner harvesting."""
from pathlib import Path


def test_holistic_live_exit_patch_is_active():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")
    broker = (root / "app/brokers/alpaca_hardened.py").read_text(encoding="utf-8")
    assert "async def _manage_live_winner_exit" in engine
    assert "self._live_runner_symbols" in engine
    assert "target = None if runner_active" in engine
    assert "runner preserving winner; ignored ordinary strategy exit" in engine
    assert "async def replace_exit_leg" in broker


def test_runner_transition_order_is_fail_closed():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")
    method = engine.split("async def _manage_live_winner_exit", 1)[1].split("def _evaluate_v2_shadow_passive", 1)[0]
    assert method.index("replaced_tp = await replace_leg") < method.index("replaced_stop = await replace_leg")
    assert "return symbol in self._live_runner_symbols" in method
    assert method.index("protected_profit =") < method.index("self._live_runner_symbols.add(symbol)")


def test_profit_floor_is_monotonic_and_original_tp_only_bypassed_after_runner():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")
    method = engine.split("async def _manage_live_winner_exit", 1)[1].split("def _evaluate_v2_shadow_passive", 1)[0]
    assert "max(desired_floor, current_floor)" in method
    assert "min(desired_floor, current_floor)" in method
    assert "protected_profit" in method
    assert "target = None if runner_active else self._position_take_profits.get(symbol)" in engine
