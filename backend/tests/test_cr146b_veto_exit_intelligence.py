import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.core.shadow_ledger import ShadowLedger
from app.strategies.evaluator import ParallelEvidenceEvaluator
from app.config import Settings
from app.main import app

def test_veto_attribution_math():
    # Test case 1: Single unique veto, trade would have lost (realized_r < 0)
    vetoes = {"ml_gate": True}
    realized_r = -1.5
    res = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, realized_r)
    # unique_veto_alpha = -realized_r = 1.5
    assert res["unique_veto_alpha"]["ml_gate"] == 1.5
    assert res["shared_veto_alpha"]["ml_gate"] == 0.0
    assert res["marginal_alpha"]["ml_gate"] == 1.5

    # Test case 2: Shared veto, trade would have won (realized_r > 0)
    vetoes = {"ml_gate": True, "meta_label_gate": True}
    realized_r = 2.0
    res = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, realized_r)
    # shared_veto_alpha = -realized_r / len(vetoes) = -2.0 / 2 = -1.0
    assert res["unique_veto_alpha"]["ml_gate"] == 0.0
    assert res["shared_veto_alpha"]["ml_gate"] == -1.0
    assert res["shared_veto_alpha"]["meta_label_gate"] == -1.0
    # Fix B1 (Issue #177): marginal_alpha for shared vetoes is now -realized_r / veto_count
    # (equal-weight Shapley approximation), not 0.0.
    assert res["marginal_alpha"]["ml_gate"] == -1.0
    assert res["marginal_alpha"]["meta_label_gate"] == -1.0

def test_chronological_replay_exit_intelligence():
    # Build dummy minute-by-minute dataframe (60 minutes)
    times = [datetime(2026, 7, 19, 9, 30) + timedelta(minutes=i) for i in range(60)]
    prices = [100.0 + i * 0.1 for i in range(60)] # moves steadily up from 100.0 to 105.9
    df = pd.DataFrame({
        "open": prices,
        "high": [p + 0.05 for p in prices],
        "low": [p - 0.05 for p in prices],
        "close": prices,
        "volume": [1000] * 60,
    }, index=pd.to_datetime(times))

    # Long trade with entry at 100.0
    # Stop loss at 99.0 (-1.0 risk), TP at 102.0 (+2.0 target)
    res = ShadowLedger.replay_signal_60m(
        df=df,
        entry_idx=0,
        direction="long",
        stop_loss=99.0,
        take_profit=102.0,
        entry_price=100.0,
    )

    assert res["tp_hit"] is True
    assert res["sl_hit"] is False
    assert res["exit_price"] == 102.0
    assert res["exit_reason"] == "tp"
    
    # R calculations: initial risk = 1.0 (max(0.2% of entry, 0.05) is 0.2)
    # entry to exit = 2.0. realized_r = 2.0
    assert res["realized_r"] == 2.0
    assert res["winner_capture_ratio"] > 0.0
    assert res["mfe_r"] >= res["realized_r"]
    assert res["time_to_mfe"] > 0

def test_shadow_ledger_endpoints(monkeypatch):
    client = TestClient(app, raise_server_exceptions=True)
    
    # Mock get_shadow_signals database call to return a list containing mock payloads
    mock_signals = [
        {
            "candidate_id": "cand_1",
            "timestamp": "2026-07-19T09:30:00",
            "symbol": "AAPL",
            "strategy": "OpeningRangeBreakout",
            "direction": "long",
            "final_decision": "rejected",
            "replayed": True,
            "simulated_pnl": -1.5,
            "mfe_pct": 0.5,
            "mae_pct": 2.0,
            "exit_reason": "sl",
            "gate_evidence_json": {
                "vetoes": ["ml_gate"]
            },
            "unique_veto_alpha": {"ml_gate": 1.5},
            "shared_veto_alpha": {"ml_gate": 0.0},
            "marginal_alpha": {"ml_gate": 1.5},
            "alternative_exits": {
                "exit_production": {
                    "pnl_pct": -1.5,
                    "realized_r": -1.5,
                    "exit_reason": "sl",
                    "bars_held": 10,
                    "mfe_r": 0.5,
                    "mae_r": 2.0
                },
                "exit_bracket_tight": {
                    "pnl_pct": 1.0,
                    "realized_r": 1.0,
                    "exit_reason": "tp",
                    "bars_held": 5,
                    "mfe_r": 1.0,
                    "mae_r": 0.5
                }
            }
        }
    ]
    
    # Mocking Database methods
    class MockDB:
        def get_shadow_signals(self, mode, limit=5000):
            return mock_signals

    class MockEngine:
        mode = "demo"
        risk_config = SimpleNamespace(
            alternative_exits=[
                {"policy_id": "exit_bracket_tight", "take_profit_pct": 1.0, "stop_loss_pct": 0.5}
            ]
        )

    monkeypatch.setattr("app.api.routes._db", lambda r: MockDB())
    monkeypatch.setattr("app.api.routes._engine", lambda r: MockEngine())

    # Verify GET /api/shadow-ledger/gate-alpha
    res_gate = client.get("/api/shadow-ledger/gate-alpha")
    assert res_gate.status_code == 200
    data_gate = res_gate.json()
    # Fix C8 (Issue #199): gate-alpha response is now wrapped in 'gates' key.
    assert "gates" in data_gate
    assert "ml_gate" in data_gate["gates"]
    assert data_gate["gates"]["ml_gate"]["unique_vetoes"] == 1
    assert data_gate["gates"]["ml_gate"]["unique_veto_alpha"] == 1.5

    # Verify GET /api/shadow-ledger/analytics
    res_an = client.get("/api/shadow-ledger/analytics")
    assert res_an.status_code == 200
    data_an = res_an.json()
    assert "alternative_exits_comparison" in data_an
    alt_list = data_an["alternative_exits_comparison"]
    assert len(alt_list) == 2
    # Baseline comparison check
    prod_exit = next(x for x in alt_list if x["policy_id"] == "exit_production")
    assert prod_exit["avg_realized_r"] == -1.5
    tight_exit = next(x for x in alt_list if x["policy_id"] == "exit_bracket_tight")
    assert tight_exit["avg_realized_r"] == 1.0
