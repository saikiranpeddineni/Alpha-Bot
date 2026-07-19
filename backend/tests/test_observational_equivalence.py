from __future__ import annotations

import json
from datetime import UTC, datetime
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

from app.core.domain import Regime, Signal, SignalAction, utcnow
from app.config import RiskConfig, Settings
from app.db.database import Database, ShadowLedgerRow
from app.strategies.evaluator import ParallelEvidenceEvaluator
from app.strategies.pipeline import SignalPipeline
from app.ai.model import SignalModel
from tests.conftest import make_bars


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test_shadow.db"
    return Database(db_path)


def test_parallel_evaluator_observational_equivalence(risk_config: RiskConfig, tmp_path: Path):
    # Setup SignalModel and pipeline
    model = SignalModel(models_dir=tmp_path)
    pipeline = SignalPipeline(model, risk_config.ml_confidence_threshold)
    evaluator = ParallelEvidenceEvaluator(
        model=model,
        ml_threshold=risk_config.ml_confidence_threshold,
        meta_label_engine=pipeline.meta_label_engine,
        trade_score_engine=pipeline.trade_score_engine,
    )

    # Make deterministic bars
    df = make_bars(n=100)
    
    # Create a candidate enter long signal
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.8,
        strategy="orb",
        reason="Breakout upward",
        price=100.5,
        stop_loss=99.0,
        take_profit=103.5,
    )
    
    # Check that calling evaluator does NOT mutate the signal fields
    sig_copy = sig.model_copy(deep=True)
    
    evidence = evaluator.evaluate(
        symbol="AAPL",
        df=df,
        sig=sig,
        regime=Regime.RANGE,
        sector_strength=0.6,
        is_event_day=False,
        midday_active=False,
    )
    
    # Verify no mutation occurred on the original signal object
    assert sig.take_profit == sig_copy.take_profit
    assert sig.stop_loss == sig_copy.stop_loss
    assert sig.ml_probability == sig_copy.ml_probability
    assert sig.score == sig_copy.score
    
    # Verify evidence structure
    assert "ml_probability" in evidence
    assert "ml_passed" in evidence
    assert "meta_probability" in evidence
    assert "meta_passed" in evidence
    assert "trade_score" in evidence
    assert "trade_score_passed" in evidence
    assert "vetoes" in evidence
    assert "veto_count" in evidence


def test_veto_attribution_logic():
    # 1. Unique Veto of a winner (+3.0R realized)
    # ML failed, others passed
    vetoes = {"ml_gate": "ML confidence < threshold"}
    attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, 3.0)
    assert attrib["unique_veto_alpha"]["ml_gate"] == -3.0
    assert attrib["unique_veto_alpha"]["meta_label_gate"] == 0.0
    assert attrib["marginal_alpha"]["ml_gate"] == -3.0
    assert attrib["shared_veto_alpha"]["ml_gate"] == 0.0
    assert attrib["coalition_veto_count"] == 1

    # 2. Unique Veto of a loser (-1.5R realized)
    # ML failed, others passed
    attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, -1.5)
    assert attrib["unique_veto_alpha"]["ml_gate"] == 1.5
    assert attrib["marginal_alpha"]["ml_gate"] == 1.5
    assert attrib["coalition_veto_count"] == 1

    # 3. Shared Veto of a winner (+3.0R realized)
    # ML and Meta Label failed
    vetoes = {
        "ml_gate": "ML confidence < threshold",
        "meta_label_gate": "Meta-label rejected",
    }
    attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, 3.0)
    assert attrib["unique_veto_alpha"]["ml_gate"] == 0.0
    assert attrib["shared_veto_alpha"]["ml_gate"] == -1.5
    assert attrib["shared_veto_alpha"]["meta_label_gate"] == -1.5
    # Fix B1 (Issue #177): marginal_alpha for shared vetoes is now -realized_r / veto_count
    # (equal-weight Shapley approximation). For 2 vetoes of a +3.0R trade: -3.0/2 = -1.5
    assert attrib["marginal_alpha"]["ml_gate"] == -1.5
    assert attrib["marginal_alpha"]["meta_label_gate"] == -1.5
    assert attrib["coalition_veto_count"] == 2

    # 4. Redundant Veto of a loser (-1.0R realized)
    # ML, Meta Label, and Trade Score failed
    vetoes = {
        "ml_gate": "ML confidence < threshold",
        "meta_label_gate": "Meta-label rejected",
        "trade_score_gate": "Score < threshold",
    }
    attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, -1.0)
    assert attrib["unique_veto_alpha"]["ml_gate"] == 0.0
    assert attrib["shared_veto_alpha"]["ml_gate"] == 0.3333
    assert attrib["shared_veto_alpha"]["meta_label_gate"] == 0.3333
    assert attrib["shared_veto_alpha"]["trade_score_gate"] == 0.3333
    # Fix B1 (Issue #177): marginal_alpha for shared vetoes is now equal-weight Shapley:
    # -realized_r / veto_count = -(-1.0)/3 = 0.3333 (previously was always 0.0)
    assert attrib["marginal_alpha"]["ml_gate"] == 0.3333
    assert attrib["coalition_veto_count"] == 3


def test_database_lineage_and_evidence_persistence(temp_db: Database):
    from app.core.shadow_ledger import ShadowLedger
    
    # Verify columns exist via DB query
    with temp_db.engine.connect() as conn:
        cursor = conn.exec_driver_sql("PRAGMA table_info(shadow_ledger)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "policy_id" in cols
        assert "parent_policy_id" in cols
        assert "policy_version" in cols
        assert "gate_evidence_json" in cols

    df = make_bars(n=50)
    spy_df = make_bars(n=50)
    
    ts = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
    
    gate_evidence = {
        "ml_probability": 0.58,
        "ml_passed": True,
        "meta_passed": False,
        "vetoes": {"meta_label_gate": "Meta-label rejected"},
        "veto_count": 1,
    }

    # Record candidate
    payload = ShadowLedger.record_candidate(
        db_engine=temp_db,
        symbol="MSFT",
        ts=ts,
        strategy="orb",
        direction="long",
        signal_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        ml_confidence=0.58,
        final_decision="rejected",
        rejection_stage="metalabel_gate",
        rejection_reason="Meta-label rejected",
        df=df,
        spy_df=spy_df,
        slot_rvol_val=1.2,
        vwap_telemetry={"vwap_state": "above"},
        mode="backtest",
        candidate_id="TEST_CANDIDATE_123",
        strategy_params={"param1": 10},
        policy_id="champion",
        parent_policy_id="parent_policy_456",
        policy_version="1.0.0",
        gate_evidence_json=gate_evidence,
    )

    # Get from DB and verify fields
    loaded = temp_db.get_shadow_signal("TEST_CANDIDATE_123")
    assert loaded is not None
    assert loaded["policy_id"] == "champion"
    assert loaded["parent_policy_id"] == "parent_policy_456"
    assert loaded["policy_version"] == "1.0.0"
    
    # Check gate evidence json
    loaded_evidence = loaded["gate_evidence_json"]
    if isinstance(loaded_evidence, str):
        loaded_evidence = json.loads(loaded_evidence)
    assert loaded_evidence["ml_probability"] == 0.58
    assert loaded_evidence["veto_count"] == 1
    
    # Check that attribution was computed during record_candidate based on outcomes
    # MSFT had stop_loss=99.0, entry_price=100.0, risk=1.0. Let's see if 15m outcome R is loaded.
    assert "unique_veto_alpha" in loaded
    assert loaded["coalition_veto_count"] == 1
    assert loaded["unique_veto_alpha"]["meta_label_gate"] is not None


async def test_evaluator_failure_fail_open(tmp_path):
    from app.config import Settings
    from app.core.engine import TradingEngine
    from app.db.database import Database
    from unittest.mock import MagicMock
    
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "engine.db"),
        DEFAULT_WATCHLIST="SPY,NVDA",
        DEMO_BAR_SECONDS=3600,
        RISK_PER_TRADE_PCT=1.0,
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)
    await engine.start("demo")
    
    # Mock evaluator to raise an exception
    engine.evidence_evaluator.evaluate = MagicMock(side_effect=Exception("Evaluator simulated error"))
    
    # Assert that _tick runs successfully without raising the exception
    try:
        await engine._tick()
    except Exception as exc:
        pytest.fail(f"TradingEngine._tick failed instead of failing open: {exc}")
        
    await engine.shutdown()


def test_attribution_recomputation_at_replay(temp_db: Database):
    ts = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
    gate_evidence = {
        "ml_probability": 0.58,
        "ml_passed": True,
        "meta_passed": False,
        "vetoes": {"meta_label_gate": "Meta-label rejected"},
        "veto_count": 1,
    }
    
    payload = {
        "candidate_id": "REPLAY_TEST_123",
        "symbol": "AAPL",
        "strategy": "vwap_reversion",
        "direction": "long",
        "outcomes": {},  # empty at first
        "gate_evidence_json": gate_evidence,
    }
    
    # First save (outcomes empty)
    temp_db.save_shadow_signal(
        candidate_id="REPLAY_TEST_123",
        ts=ts,
        symbol="AAPL",
        strategy="vwap_reversion",
        direction="long",
        mode="backtest",
        payload=payload,
    )
    
    loaded = temp_db.get_shadow_signal("REPLAY_TEST_123")
    assert loaded["unique_veto_alpha"]["meta_label_gate"] == 0.0
    
    # Second save (outcomes filled)
    payload["outcomes"] = {
        "15m": {"realized_r": 3.0}
    }
    
    temp_db.save_shadow_signal(
        candidate_id="REPLAY_TEST_123",
        ts=ts,
        symbol="AAPL",
        strategy="vwap_reversion",
        direction="long",
        mode="backtest",
        payload=payload,
    )
    
    loaded = temp_db.get_shadow_signal("REPLAY_TEST_123")
    # For a winner (+3.0R) vetoed uniquely by meta_label_gate, unique_veto_alpha should update to -3.0
    assert loaded["unique_veto_alpha"]["meta_label_gate"] == -3.0

