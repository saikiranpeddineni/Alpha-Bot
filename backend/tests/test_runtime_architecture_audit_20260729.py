from __future__ import annotations
import pytest

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "app/core/engine.py"


def _engine_tree():
    return ast.parse(ENGINE.read_text(encoding="utf-8"))


def _methods_named(name: str):
    return [
        node for node in ast.walk(_engine_tree())
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def _tick_method():
    methods = _methods_named("_tick")
    assert len(methods) == 1
    return methods[0]


def _awaited_self_calls(method):
    names = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        owner = call.func.value
        if isinstance(owner, ast.Name) and owner.id == "self":
            names.append(call.func.attr)
    return names


def _awaited_broker_calls(method):
    names = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        owner = call.func.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr == "broker"
        ):
            names.append(call.func.attr)
    return names


def test_tick_polls_real_broker_fills_once_per_cycle():
    """One tick should have one canonical real-broker fill poll/drain phase."""
    broker_calls = _awaited_broker_calls(_tick_method())
    assert broker_calls.count("poll_fills") == 1


def test_scanner_and_trainer_do_not_block_tick_owner():
    """Slow scanner/trainer work must not own the market-data/execution clock."""
    calls = _awaited_self_calls(_tick_method())
    # Pre-market scan (at 09:00 ET) is run once at most before open; continuous intraday scans and training are non-blocking tasks.
    assert calls.count("run_market_scanner") <= 1

    tick = _tick_method()
    trainer_trigger_awaits = []
    for node in ast.walk(tick):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        fn = node.value.func
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr == "trigger"
            and isinstance(fn.value, ast.Attribute)
            and isinstance(fn.value.value, ast.Name)
            and fn.value.value.id == "self"
            and fn.value.attr == "trainer"
        ):
            trainer_trigger_awaits.append(node)
    assert not trainer_trigger_awaits


def test_telemetry_does_not_fabricate_broker_ping_or_latency():
    """Rendering telemetry must not manufacture operational timestamps/latency."""
    src = ast.get_source_segment(ENGINE.read_text(encoding="utf-8"), _methods_named("telemetry_payload")[0]) or ""
    assert 'self._subsystem_timestamps["last_broker_ping"] = now_iso' not in src
    assert '"latency_ms": 15' not in src


def test_engine_init_has_single_scanner_state_initialization():
    """State ownership should be singular; duplicate scanner initialization hides lifecycle bugs."""
    init = _methods_named("__init__")[0]
    assigned = []
    for node in ast.walk(init):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr in {"scanned_watchlist", "scanned_watchlist_ttl"}
                ):
                    assigned.append(target.attr)
    assert assigned.count("scanned_watchlist") == 1
    assert assigned.count("scanned_watchlist_ttl") == 1
