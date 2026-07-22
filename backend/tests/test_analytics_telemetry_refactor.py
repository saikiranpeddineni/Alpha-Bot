import math
import pytest
from app.api.routes import _is_canonical_replay_evidence, _outcome_value
from app.core.shadow_ledger import _classify_hybrid_effect
from app.strategies.evaluator import ParallelEvidenceEvaluator


def test_shadow_summary_reports_mfe_in_r():
    # Mock payload summary matching updated routes.py schema
    response_summary = {
        "total_signals": 1807,
        "replayed_signals": 863,
        "valid_r_count": 171,
        "avg_mfe_r": 0.56,
    }
    assert response_summary["avg_mfe_r"] == pytest.approx(0.56, abs=0.01)
    assert "avg_mfe_pct" not in response_summary


def test_deferred_replay_is_not_canonical_evidence():
    signal = {
        "replayed": False,
        "replay_failed": False,
        "replay_state": "deferred_data_incomplete",
    }
    outcome = {"realized_r": 1.25}
    assert not _is_canonical_replay_evidence(signal, outcome)

    signal_failed = {
        "replayed": True,
        "replay_failed": True,
    }
    assert not _is_canonical_replay_evidence(signal_failed, outcome)


def test_gate_attribution_uses_current_canonical_outcome():
    # Signal with stale persisted marginal_alpha dict {ml_gate: 0.0}
    signal = {
        "replayed": True,
        "replay_failed": False,
        "gate_evidence_json": {
            "vetoes": {"ml_gate": "ML confidence low"}
        },
        "marginal_alpha": {"ml_gate": 0.0},
    }
    realized_r = 1.25
    attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(
        signal["gate_evidence_json"]["vetoes"],
        realized_r
    )
    assert attrib["marginal_alpha"]["ml_gate"] == pytest.approx(-1.25)


def test_shared_veto_alpha_uses_current_outcome():
    realized_r = -1.0
    vetoes = {"ml_gate": "ML low", "meta_label_gate": "Meta rejected"}
    attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(
        vetoes,
        realized_r
    )
    assert attrib["marginal_alpha"]["ml_gate"] == pytest.approx(0.5)
    assert attrib["marginal_alpha"]["meta_label_gate"] == pytest.approx(0.5)


def test_alternative_exit_common_r_uses_production_risk():
    entry = 100.0
    production_stop = 99.0
    production_risk = abs(entry - production_stop)  # 1.0

    tight_exit = 100.50
    common_r = (tight_exit - entry) / production_risk

    assert common_r == pytest.approx(0.50)


def test_alternative_exit_common_r_uses_production_risk_per_signal_invariant():
    entry = 100.0
    production_stop = 98.50   # Production risk = $1.50 (1.5%)
    tight_stop = 99.50        # Tight risk = $0.50 (0.5%)
    tight_exit = 100.30

    tight_native_r = (tight_exit - entry) / (entry - tight_stop)
    tight_common_r = (tight_exit - entry) / (entry - production_stop)

    assert tight_native_r == pytest.approx(0.60)
    assert tight_common_r == pytest.approx(0.20)



def test_hybrid_reduced_winner_classification():
    assert _classify_hybrid_effect(
        production_r=1.50,
        hybrid_r=0.05,
    ) == "HYBRID_REDUCED_WINNER"


def test_hybrid_saved_loss_classification():
    assert _classify_hybrid_effect(
        production_r=-1.0,
        hybrid_r=0.0,
    ) == "HYBRID_SAVED_LOSS"


def test_hybrid_worsened_loss_classification():
    assert _classify_hybrid_effect(
        production_r=-0.5,
        hybrid_r=-1.0,
    ) == "HYBRID_WORSENED_LOSS"


def test_hybrid_delta_reconciliation():
    # Simulate a series of trades with production_r and hybrid_r
    trades = [
        {"prod": -1.0, "hybrid": 0.0},    # Saved loss: +1.0R
        {"prod": 0.5, "hybrid": 1.0},     # Improved winner: +0.5R
        {"prod": 1.5, "hybrid": 0.5},     # Reduced winner: -1.0R
        {"prod": -0.5, "hybrid": -0.8},   # Worsened loss: -0.3R
        {"prod": 1.0, "hybrid": 1.0},     # No effect: 0.0R
    ]

    saved_loss_r = 0.0
    improved_winner_r = 0.0
    reduced_winner_r = 0.0
    worsened_loss_r = 0.0
    net_delta_r = 0.0

    deltas = []
    for t in trades:
        p_r = t["prod"]
        h_r = t["hybrid"]
        d = h_r - p_r
        deltas.append(d)
        net_delta_r += d

        eff = _classify_hybrid_effect(p_r, h_r)
        if eff == "HYBRID_SAVED_LOSS":
            saved_loss_r += d
        elif eff == "HYBRID_IMPROVED_WINNER":
            improved_winner_r += d
        elif eff == "HYBRID_REDUCED_WINNER":
            reduced_winner_r += -d
        elif eff == "HYBRID_WORSENED_LOSS":
            worsened_loss_r += -d

    category_sum = saved_loss_r + improved_winner_r - reduced_winner_r - worsened_loss_r

    assert category_sum == pytest.approx(net_delta_r)
    assert sum(deltas) == pytest.approx(net_delta_r)

