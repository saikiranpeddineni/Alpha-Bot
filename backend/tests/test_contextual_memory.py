from __future__ import annotations

from datetime import UTC, datetime
import json
import pytest
from pathlib import Path

from app.core.domain import Regime, SignalAction, utcnow
from app.db.database import Database, ContextualPerformanceMemoryRow
from app.strategies.memory import (
    get_rvol_bucket,
    get_vol_bucket,
    calculate_shrunk_expectancy,
    refresh_contextual_memory,
    get_contextual_expectancy,
)
from tests.conftest import make_bars


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test_memory.db"
    return Database(db_path)


def test_bucketing_helpers():
    # RVOL Bucketing
    assert get_rvol_bucket(None) == "normal"
    assert get_rvol_bucket(0.3) == "low"
    assert get_rvol_bucket(0.8) == "normal"
    assert get_rvol_bucket(1.4) == "above_normal"
    assert get_rvol_bucket(2.5) == "high"

    # Volatility Bucketing
    assert get_vol_bucket(None) == "normal"
    assert get_vol_bucket(0.02) == "low"
    assert get_vol_bucket(0.08) == "normal"
    assert get_vol_bucket(0.18) == "high"


def test_bayesian_shrinkage():
    # Local: 2 trades, total R = 4.0 (Expectancy = 2.0)
    # Global: 100 trades, total R = 50.0 (Expectancy = 0.5)
    # K = 10.0 prior strength
    # Expectancy_shrunk = (4.0 + 10.0 * 0.5) / (2 + 10) = 9.0 / 12 = 0.75
    shrunk = calculate_shrunk_expectancy(local_r=4.0, local_n=2, global_r=50.0, global_n=100, K=10.0)
    assert shrunk == 0.75

    # If global_n is zero
    shrunk_zero_global = calculate_shrunk_expectancy(local_r=4.0, local_n=2, global_r=0.0, global_n=0, K=10.0)
    assert shrunk_zero_global == 4.0 / 12.0


def test_database_performance_memory_row_creation(temp_db: Database):
    # Verify table schema is created on database initialization
    with temp_db.engine.connect() as conn:
        cursor = conn.exec_driver_sql("PRAGMA table_info(contextual_performance_memory)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "strategy" in cols
        assert "direction" in cols
        assert "regime" in cols
        assert "clock_phase" in cols
        assert "rvol_bucket" in cols
        assert "vol_bucket" in cols
        assert "trades_count" in cols
        assert "shrunk_expectancy" in cols


def test_contextual_memory_compilation_and_retrieval(temp_db: Database):
    from app.core.shadow_ledger import ShadowLedger

    # 1. Clear aggregates first
    temp_db.clear_context_aggregates()

    # 2. Insert shadow signal records to simulate completed trades
    ts = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
    df = make_bars(n=200)
    spy_df = make_bars(n=200)

    # Signal 1: Winner (+3R) in Range regime, normal RVOL, normal VOL
    ShadowLedger.record_candidate(
        db_engine=temp_db,
        symbol="AAPL",
        ts=ts,
        strategy="orb",
        direction="long",
        signal_price=100.0,
        stop_loss=99.0,
        take_profit=103.0, # 3R take profit
        ml_confidence=0.6,
        final_decision="pipeline_approved",
        rejection_stage="none",
        rejection_reason="",
        df=df,
        spy_df=spy_df,
        slot_rvol_val=0.8, # maps to "normal"
        vwap_telemetry={},
        mode="backtest",
        candidate_id="CAND_1",
        spy_vol=0.06, # maps to "normal"
    )

    # Signal 2: Winner (+3R) in same context
    ShadowLedger.record_candidate(
        db_engine=temp_db,
        symbol="MSFT",
        ts=ts,
        strategy="orb",
        direction="long",
        signal_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        ml_confidence=0.6,
        final_decision="pipeline_approved",
        rejection_stage="none",
        rejection_reason="",
        df=df,
        spy_df=spy_df,
        slot_rvol_val=0.9, # maps to "normal"
        vwap_telemetry={},
        mode="backtest",
        candidate_id="CAND_2",
        spy_vol=0.07, # maps to "normal"
    )

    # Compile contextual aggregates from the database
    refresh_contextual_memory(temp_db)

    # Check database save
    key = {
        "strategy": "orb",
        "direction": "long",
        "regime": "RANGE",
        "clock_phase": "morning",
        "rvol_bucket": "normal",
        "vol_bucket": "normal",
    }
    agg = temp_db.get_context_aggregate(key)
    assert agg is not None
    assert agg["trades_count"] == 2
    assert agg["wins_count"] == 0
    
    # Global expectancy: 2 trades, total R = -0.3106 (avg = -0.1553)
    # Local expectancy: 2 trades, total R = -0.3106
    # Shrunk expectancy: (-0.3106 + 10.0 * -0.1553) / (2 + 10) = -1.8636 / 12 = -0.1553
    assert agg["shrunk_expectancy"] == -0.1553

    # Test get_contextual_expectancy query
    lookup = get_contextual_expectancy(
        db=temp_db,
        strategy="orb",
        direction="long",
        regime="RANGE",
        clock_phase="morning",
        rvol=0.8,
        spy_vol=0.06,
    )
    assert lookup["source"] == "contextual_memory"
    assert lookup["trades_count"] == 2
    assert lookup["expectancy"] == -0.1553
    assert lookup["shrunk_expectancy"] == -0.1553

    # Test fallback query for unseen context
    lookup_fallback = get_contextual_expectancy(
        db=temp_db,
        strategy="momentum",
        direction="short",
        regime="TREND_UP",
        clock_phase="open",
        rvol=2.5,
        spy_vol=0.20,
    )
    assert lookup_fallback["source"] == "default_prior"
    assert lookup_fallback["shrunk_expectancy"] == 0.0
