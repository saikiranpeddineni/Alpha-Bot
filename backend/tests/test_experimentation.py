from __future__ import annotations

from datetime import UTC, datetime
import pytest
from pathlib import Path

from app.core.domain import Regime, SignalAction
from app.db.database import Database
from app.strategies.challenger import get_active_challengers
from app.strategies.memory import get_episodic_nearest_neighbors
from app.strategies.reflection import generate_eod_reflection_report
from app.core.shadow_ledger import ShadowLedger
from tests.conftest import make_bars


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test_experimentation.db"
    return Database(db_path)


def test_challenger_configs():
    challengers = get_active_challengers()
    assert len(challengers) == 3
    assert challengers[0].policy_id == "challenger_low_ml"
    assert challengers[0].ml_threshold == 0.48
    
    assert challengers[1].policy_id == "challenger_high_ml"
    assert challengers[1].ml_threshold == 0.56

    assert challengers[2].policy_id == "challenger_aggressive_score"
    assert challengers[2].ml_threshold == 0.50
    assert challengers[2].score_threshold_offset == -10.0


def test_level2_nearest_neighbors(temp_db: Database):
    # 1. Clear database candidates
    # Setup some test candidates in ShadowLedger
    ts = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
    df = make_bars(n=200)
    spy_df = make_bars(n=200)

    # Insert 4 historical candidates with distinct continuous features
    # Candidate 1: rvol=0.5, spy_vol=0.04, vwap=0.1, ml=0.45
    ShadowLedger.record_candidate(
        db_engine=temp_db,
        symbol="AAPL",
        ts=ts,
        strategy="orb",
        direction="long",
        signal_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        ml_confidence=0.45,
        final_decision="pipeline_approved",
        rejection_stage="none",
        rejection_reason="",
        df=df,
        spy_df=spy_df,
        slot_rvol_val=0.5,
        vwap_telemetry={"vwap_distance_atr": 0.1},
        mode="backtest",
        candidate_id="CAND_1",
        spy_vol=0.04,
    )

    # Candidate 2: rvol=2.0, spy_vol=0.15, vwap=-0.5, ml=0.65
    ShadowLedger.record_candidate(
        db_engine=temp_db,
        symbol="MSFT",
        ts=ts,
        strategy="orb",
        direction="long",
        signal_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        ml_confidence=0.65,
        final_decision="pipeline_approved",
        rejection_stage="none",
        rejection_reason="",
        df=df,
        spy_df=spy_df,
        slot_rvol_val=2.0,
        vwap_telemetry={"vwap_distance_atr": -0.5},
        mode="backtest",
        candidate_id="CAND_2",
        spy_vol=0.15,
    )

    # Search for nearest neighbors to target vector: [0.6, 0.05, 0.15, 0.48] (Close to Candidate 1)
    target = {
        "slot_rvol": 0.6,
        "spy_vol": 0.05,
        "vwap_distance_atr": 0.15,
        "ml_confidence": 0.48,
    }

    neighbors = get_episodic_nearest_neighbors(db=temp_db, current_features=target, k=2)
    assert len(neighbors) == 2
    # CAND_1 must be the closest (smallest distance)
    assert neighbors[0]["candidate_id"] == "CAND_1"
    assert neighbors[1]["candidate_id"] == "CAND_2"
    assert neighbors[0]["distance"] < neighbors[1]["distance"]


def test_generate_eod_reflection_report(temp_db: Database):
    # Setup completed signals in database
    ts = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
    df = make_bars(n=200)
    spy_df = make_bars(n=200)

    # Champion candidate 1 (Winner)
    ShadowLedger.record_candidate(
        db_engine=temp_db,
        symbol="AAPL",
        ts=ts,
        strategy="orb",
        direction="long",
        signal_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        ml_confidence=0.60,
        final_decision="pipeline_approved",
        rejection_stage="none",
        rejection_reason="",
        df=df,
        spy_df=spy_df,
        slot_rvol_val=1.0,
        vwap_telemetry={},
        mode="backtest",
        candidate_id="SHARED_1",
        policy_id="champion",
        spy_vol=0.06,
    )

    # Challenger candidate 1 (Winner)
    ShadowLedger.record_candidate(
        db_engine=temp_db,
        symbol="AAPL",
        ts=ts,
        strategy="orb",
        direction="long",
        signal_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        ml_confidence=0.60,
        final_decision="pipeline_approved",
        rejection_stage="none",
        rejection_reason="",
        df=df,
        spy_df=spy_df,
        slot_rvol_val=1.0,
        vwap_telemetry={},
        mode="challenger",
        candidate_id="SHARED_1",
        policy_id="challenger_low_ml",
        spy_vol=0.06,
    )

    # Run EOD reflection report
    report = generate_eod_reflection_report(temp_db)
    
    assert "EOD Policy Performance Reflection Report" in report
    assert "champion" in report
    assert "challenger_low_ml" in report
    assert "Approved" in report
    assert "Expectancy" in report
