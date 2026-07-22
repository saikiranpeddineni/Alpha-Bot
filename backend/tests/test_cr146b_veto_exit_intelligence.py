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


def test_gate_alpha_handles_none_realized_r(monkeypatch):
    """Ensure signals with outcomes that yield None from _canonical_realized_r do not throw TypeError."""
    client = TestClient(app, raise_server_exceptions=True)
    
    mock_signals = [
        {
            "candidate_id": "cand_null_r",
            "timestamp": "2026-07-19T09:30:00",
            "symbol": "TSLA",
            "strategy": "OpeningRangeBreakout",
            "direction": "long",
            "final_decision": "rejected",
            "outcomes": {"some_unrecognized_key": 123},  # causes _canonical_realized_r to return None
            "gate_evidence_json": {
                "vetoes": ["ml_gate"]
            },
            "unique_veto_alpha": {"ml_gate": 0.0},
        }
    ]

    class MockDB:
        def get_shadow_signals(self, mode, limit=5000):
            return mock_signals

    class MockEngine:
        mode = "demo"

    monkeypatch.setattr("app.api.routes._db", lambda r: MockDB())
    monkeypatch.setattr("app.api.routes._engine", lambda r: MockEngine())

    res = client.get("/api/shadow-ledger/gate-alpha")
    assert res.status_code == 200
    data = res.json()
    assert "gates" in data
    # Non-canonical null-R evidence signals are strictly excluded from gate-alpha attribution
    assert data["gates"]["ml_gate"]["unique_vetoes"] == 0
    assert data["gates"]["ml_gate"]["avoided_losses"] == 0
    assert data["gates"]["ml_gate"]["missed_profits"] == 0



def test_first_decisive_gate_attribution():
    """Verify first_decisive_gate is populated and recorded in veto attribution."""
    vetoes = {"ml_gate": "ML low", "meta_label_gate": "Meta low"}
    res = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, -1.0)
    assert res["first_decisive_gate"] == "ml_gate"
    assert res["coalition_veto_count"] == 2


def test_hybrid_breakeven_atr_replay():
    """Verify exit_hybrid_breakeven_atr moves stop to breakeven after +0.30R MFE."""
    times = [datetime(2026, 7, 19, 9, 30) + timedelta(minutes=i) for i in range(20)]
    # Prices: 100 -> 100.4 (MFE +0.4R on 1.0 risk) -> 99.8 (pullback)
    prices = [100.0, 100.2, 100.4, 100.2, 99.8] + [99.8] * 15
    df = pd.DataFrame({
        "open": prices,
        "high": [p + 0.05 for p in prices],
        "low": [p - 0.05 for p in prices],
        "close": prices,
        "volume": [1000] * 20,
        "atr14": [0.2] * 20,
    }, index=pd.to_datetime(times))

    res = ShadowLedger.replay_signal_60m(
        df=df,
        entry_idx=0,
        direction="long",
        stop_loss=99.0,  # 1.0 initial risk
        take_profit=None,
        entry_price=100.0,
        strategy_params={
            "breakeven_r_trigger": 0.30,
            "trail_r_trigger": 0.75,
            "tight_trail_r_trigger": 1.50,
        }
    )

    assert res["exit_reason"] in ("trailing_stop", "sl", "hybrid_breakeven_stop")
    # Because breakeven triggered at +0.30R MFE (100.3+), exit_price should be breakeven (>= 100.0), not initial stop (99.0)
    assert res["exit_price"] >= 100.0


def test_replay_optimizer_grid_search():
    """Verify ReplayOptimizer ranks parameter grids and finds best config."""
    from app.analytics.replay_optimizer import ReplayOptimizer
    mock_signals = [
        {
            "ml_probability": 0.52,
            "meta_probability": 0.55,
            "trade_score": 65.0,
            "outcomes": {
                "exit_hybrid_breakeven_atr": {"realized_r": 1.5, "mfe_r": 1.8, "gave_back_r": 0.3, "winner_capture_ratio": 0.83},
                "exit_production": {"realized_r": 0.1, "mfe_r": 1.8, "gave_back_r": 1.7, "winner_capture_ratio": 0.05},
            }
        },
        {
            "ml_probability": 0.49,
            "meta_probability": 0.51,
            "trade_score": 58.0,
            "outcomes": {
                "exit_hybrid_breakeven_atr": {"realized_r": 0.8, "mfe_r": 1.0, "gave_back_r": 0.2, "winner_capture_ratio": 0.80},
                "exit_production": {"realized_r": -1.0, "mfe_r": 0.2, "gave_back_r": 0.2, "winner_capture_ratio": 0.0},
            }
        }
    ]

    optimizer = ReplayOptimizer(
        ml_thresholds=[0.48, 0.55],
        exit_policies=["exit_production", "exit_hybrid_breakeven_atr"],
        composite_thresholds=[None],
    )
    res = optimizer.optimize(mock_signals)

    assert res["total_signals_evaluated"] == 2
    assert len(res["results"]) == 4  # 2 thresholds * 2 policies
    best = res["best_config"]
    assert best["exit_policy"] == "exit_hybrid_breakeven_atr"
    assert best["ml_threshold"] == 0.48
    assert best["total_realized_r"] == 2.3


def test_parse_datetime_to_utc_naive_preserves_utc():
    """Verify parse_datetime_to_utc treats naive datetimes as UTC and does not apply ET offsets."""
    from app.core.shadow_replay_eligibility import parse_datetime_to_utc
    from datetime import datetime, UTC

    naive_dt = datetime(2026, 7, 21, 19, 19, 50)
    parsed = parse_datetime_to_utc(naive_dt)

    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed.hour == 19
    assert parsed.minute == 19
    assert parsed.second == 50


def test_parse_datetime_to_utc_candidate_id_shift_correction():
    """Verify parse_datetime_to_utc auto-corrects +4h timestamp shifts using candidate_id string."""
    from app.core.shadow_replay_eligibility import parse_datetime_to_utc
    from datetime import datetime, UTC

    # Database contains shifted timestamp 23:19:50 from prior ET double conversion
    shifted_dt = datetime(2026, 7, 21, 23, 19, 50, tzinfo=UTC)
    candidate_id = "NOW_RSI2_REVERSION_20260721_191950_79EECE"

    parsed = parse_datetime_to_utc(shifted_dt, candidate_id=candidate_id)

    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed.hour == 19
    assert parsed.minute == 19
    assert parsed.second == 50


def test_parse_datetime_to_utc_plus_5h_shift_correction():
    """Verify parse_datetime_to_utc auto-corrects +5h (EST) timestamp shifts crossing date boundary."""
    from app.core.shadow_replay_eligibility import parse_datetime_to_utc
    from datetime import datetime, UTC

    # Database contains +5h shifted timestamp 00:19:50 next day from EST double conversion
    shifted_dt = datetime(2026, 1, 21, 0, 19, 50, tzinfo=UTC)
    candidate_id = "NOW_RSI2_REVERSION_20260120_191950_79EECE"

    parsed = parse_datetime_to_utc(shifted_dt, candidate_id=candidate_id)

    assert parsed is not None
    assert parsed == datetime(2026, 1, 20, 19, 19, 50, tzinfo=UTC)


def test_parse_datetime_to_utc_negative_shift_not_corrected():
    """Verify parse_datetime_to_utc does NOT correct negative shifts (-4h / -5h)."""
    from app.core.shadow_replay_eligibility import parse_datetime_to_utc
    from datetime import datetime, UTC

    # Stored timestamp is 15:19:50 (4 hours earlier than candidate 19:19:50)
    neg_shifted_dt = datetime(2026, 7, 21, 15, 19, 50, tzinfo=UTC)
    candidate_id = "NOW_RSI2_REVERSION_20260721_191950_79EECE"

    parsed = parse_datetime_to_utc(neg_shifted_dt, candidate_id=candidate_id)

    assert parsed is not None
    assert parsed == neg_shifted_dt  # Kept unchanged, NOT shifted to 19:19:50


def test_parse_datetime_to_utc_normal_timestamps_unchanged():
    """Verify normal timestamps (matching or uncorrupted differences) remain unchanged."""
    from app.core.shadow_replay_eligibility import parse_datetime_to_utc
    from datetime import datetime, UTC

    candidate_id = "NOW_RSI2_REVERSION_20260721_191950_79EECE"
    
    # Exact match
    matching_dt = datetime(2026, 7, 21, 19, 19, 50, tzinfo=UTC)
    assert parse_datetime_to_utc(matching_dt, candidate_id=candidate_id) == matching_dt

    # Unrelated offset (+3h shift, not legacy +4h/+5h)
    unrelated_dt = datetime(2026, 7, 21, 22, 19, 50, tzinfo=UTC)
    assert parse_datetime_to_utc(unrelated_dt, candidate_id=candidate_id) == unrelated_dt


def test_parse_datetime_to_utc_malformed_and_missing_candidate_ids():
    """Verify parse_datetime_to_utc handles malformed candidate IDs and IDs without timestamps gracefully."""
    from app.core.shadow_replay_eligibility import parse_datetime_to_utc
    from datetime import datetime, UTC

    dt = datetime(2026, 7, 21, 19, 19, 50, tzinfo=UTC)

    # Missing timestamp in ID
    assert parse_datetime_to_utc(dt, candidate_id="CANDIDATE_NO_TIMESTAMP") == dt

    # Malformed timestamp in ID (invalid date)
    assert parse_datetime_to_utc(dt, candidate_id="CANDIDATE_20269999_999999_79EECE") == dt

    # Non-string candidate_id
    assert parse_datetime_to_utc(dt, candidate_id=12345) == dt


def test_parse_datetime_to_utc_iso_z_and_et_tz():
    """Verify parse_datetime_to_utc handles ISO 'Z' strings and timezone-aware ET datetimes."""
    from app.core.shadow_replay_eligibility import parse_datetime_to_utc
    from datetime import datetime, UTC
    from zoneinfo import ZoneInfo

    candidate_id = "NOW_RSI2_REVERSION_20260721_191950_79EECE"

    # ISO Z string
    iso_z_str = "2026-07-21T19:19:50Z"
    parsed_z = parse_datetime_to_utc(iso_z_str, candidate_id=candidate_id)
    assert parsed_z == datetime(2026, 7, 21, 19, 19, 50, tzinfo=UTC)

    # Timezone-aware ET datetime (15:19:50 EDT = 19:19:50 UTC)
    et_dt = datetime(2026, 7, 21, 15, 19, 50, tzinfo=ZoneInfo("America/New_York"))
    parsed_et = parse_datetime_to_utc(et_dt, candidate_id=candidate_id)
    assert parsed_et == datetime(2026, 7, 21, 19, 19, 50, tzinfo=UTC)


def test_parse_datetime_to_utc_midnight_date_boundary():
    """Verify parse_datetime_to_utc handles +4h/+5h shifts that roll over midnight across calendar dates."""
    from app.core.shadow_replay_eligibility import parse_datetime_to_utc
    from datetime import datetime, UTC

    candidate_id = "STRAT_20260721_213000_123456"
    # Corrupted +4h shift moves 21:30:00 UTC on July 21 to 01:30:00 UTC on July 22
    corrupted_midnight_dt = datetime(2026, 7, 22, 1, 30, 0, tzinfo=UTC)

    parsed = parse_datetime_to_utc(corrupted_midnight_dt, candidate_id=candidate_id)
    assert parsed == datetime(2026, 7, 21, 21, 30, 0, tzinfo=UTC)





