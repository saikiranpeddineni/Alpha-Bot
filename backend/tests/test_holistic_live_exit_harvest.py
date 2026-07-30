"""Regression invariants for PIS live exit authority."""
from pathlib import Path


def test_pis_live_exit_cutover_is_active():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")
    assert "PISLiveExitController" in engine
    assert "self._pis_exit = PISLiveExitController()" in engine
    assert "async def _evaluate_pis_for_position" in engine
    assert "evaluation_payload(evaluation)" in engine
    assert "PIS EXIT" in engine


def test_legacy_live_runner_is_no_longer_the_authority_layer():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")
    method = engine.split("async def _manage_live_winner_exit", 1)[1].split("async def _evaluate_pis_for_position", 1)[0]
    assert "PIS owns live winner management" in method
    assert "Legacy runner promotion is now a counterfactual baseline only" in method
    assert "self._live_runner_symbols.add(symbol)" in method
    assert "replace_exit_leg" in method


def test_pis_eval_path_is_session_and_staleness_gated():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")
    method = engine.split("async def _evaluate_pis_for_position", 1)[1].split("# broadcast payloads and remaining engine methods remain unchanged below", 1)[0]
    assert "assess_evidence_freshness" in method
    assert "session_context" in engine
    assert "stale evidence" in method
    assert "replace_exit_leg" in method
