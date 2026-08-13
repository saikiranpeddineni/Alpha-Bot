from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "app/replay/runtime.py"
SESSION = ROOT / "app/replay/session.py"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _method(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(_src(path))
    matches = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_replay_disables_shadow_replay_feedback_loop():
    src = _src(RUNTIME)
    method = ast.get_source_segment(src, _method(RUNTIME, "_initialize_engine")) or ""
    assert "disabled_shadow_replay" in method
    assert "engine._replay_pending_signals = disabled_shadow_replay" in method


def test_replay_preserves_long_market_risk_context():
    src = _src(RUNTIME)
    assert "_RISK_CONTEXT_DAYS = 65" in src
    assert "_RISK_CONTEXT_BARS = 26000" in src
    assert "ReplayDataManager(bars, max_rows=_REPLAY_DATA_MAX_ROWS)" in src
    assert "[\"SPY\", \"QQQ\"]" in src


def test_replay_warmup_is_fail_closed():
    src = _src(RUNTIME)
    method = ast.get_source_segment(src, _method(RUNTIME, "_execute_single_pass")) or ""
    assert "EngineState.SAFE_MODE" in method
    assert "engine.allow_new_entries = is_replay_window" in method


def test_replay_waits_for_detached_production_jobs_before_advancing_clock():
    src = _src(RUNTIME)
    method = ast.get_source_segment(src, _method(RUNTIME, "_execute_single_pass")) or ""
    helper = ast.get_source_segment(src, _method(RUNTIME, "_await_replay_aux_tasks")) or ""
    assert "await self._await_replay_aux_tasks(engine)" in method
    assert '"_scanner_task"' in helper
    assert '"_event_monitor_task"' in helper


def test_replay_session_cancellation_does_not_survive_session_recreation():
    src = _src(SESSION)
    method = ast.get_source_segment(src, _method(SESSION, "create_session")) or ""
    delete_method = ast.get_source_segment(src, _method(SESSION, "delete_session")) or ""
    assert "self._active_cancels.discard(replay_id)" in method
    assert "self._active_cancels.discard(replay_id)" in delete_method
