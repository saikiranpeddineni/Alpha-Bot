from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "app/core/engine.py"


def _tree():
    return ast.parse(ENGINE.read_text(encoding="utf-8"))


def _method(name: str):
    methods = [n for n in ast.walk(_tree()) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(methods) == 1
    return methods[0]


def _src(name: str) -> str:
    text = ENGINE.read_text(encoding="utf-8")
    return ast.get_source_segment(text, _method(name)) or ""


def test_shadow_replay_does_not_block_production_tick():
    tick = _method("_tick")
    for node in ast.walk(tick):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        fn = node.value.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            assert not (fn.value.id == "self" and fn.attr == "_replay_pending_signals")


def test_shadow_replay_task_has_explicit_shutdown_owner():
    init_src = _src("__init__")
    stop_src = _src("stop")
    assert "self._shadow_replay_task" in init_src
    assert '"_shadow_replay_task"' in stop_src


def test_partial_fill_does_not_mark_order_filled_unconditionally():
    src = _src("_drain_fills")
    assert "OrderStatus.PARTIALLY_FILLED" in src
    assert "cumulative_qty" in src
    assert "order.status = OrderStatus.FILLED\n" not in src


def test_pending_close_guard_release_is_side_neutral_and_flatness_based():
    src = _src("_drain_fills")
    assert "local_lot" in src
    assert "self._pending_close_symbols.discard(symbol)" in src
    assert "if fill.side == OrderSide.SELL" not in src


def test_heartbeat_emergency_flatten_has_single_task_owner():
    src = _src("_check_system_heartbeat")
    assert "self._emergency_flatten_task is None or self._emergency_flatten_task.done()" in src
    assert "self._emergency_flatten_task = asyncio.create_task" in src
