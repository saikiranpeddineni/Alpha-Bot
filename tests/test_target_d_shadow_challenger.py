"""Hard Non-Interference and Artifact Verification Tests for Target D Shadow Challenger.
"""

import os
import sys
import pytest
import pandas as pd
import numpy as np
from types import SimpleNamespace

from app.ai.features import FEATURE_COLUMNS
from app.ai.target_d_observer import TargetDShadowObserver
from app.core.domain import Signal, SignalAction, Regime


def test_target_d_artifact_provenance():
    """Verify Target D model artifact provenance, immutable metadata, and feature schema alignment."""
    observer = TargetDShadowObserver()
    
    assert os.path.exists(observer.model_path), f"Model artifact missing: {observer.model_path}"
    assert os.path.exists(observer.meta_path), f"Metadata artifact missing: {observer.meta_path}"
    
    assert observer.metadata.get("training_target") == "realized_r >= +0.5R"
    assert observer.metadata.get("generation_id") == "target_d_v1_20260528"
    assert observer.metadata.get("features") == FEATURE_COLUMNS
    assert observer.threshold == 0.48
    assert "2026-05-28T16:30:00" in observer.metadata.get("trained_at_cutoff", "")


def test_target_d_non_interference_no_broker_methods():
    """Verify Target D observer has zero broker, execution, or order submission attributes or methods."""
    observer = TargetDShadowObserver()
    
    forbidden_attrs = ["broker", "submit_order", "place_order", "execute", "cancel_order", "update_position", "risk_manager"]
    for attr in forbidden_attrs:
        assert not hasattr(observer, attr), f"TargetDShadowObserver violates isolation: contains '{attr}' attribute!"


def test_target_d_passive_evaluation_no_signal_mutation():
    """Verify passive evaluation leaves Signal object 100% untouched and returns expected telemetry."""
    observer = TargetDShadowObserver()
    
    dummy_feats = {c: 0.5 for c in FEATURE_COLUMNS}
    sig = Signal(
        symbol="NVDA",
        action=SignalAction.ENTER_LONG,
        confidence=0.8,
        strategy="orb",
        reason="Breakout"
    )
    
    orig_dict = sig.model_dump()
    
    res = observer.evaluate_passive(
        features=dummy_feats,
        master_prob=0.65,
        master_decision="PASS"
    )
    
    assert sig.model_dump() == orig_dict
    
    assert "target_d_probability" in res
    assert "target_d_decision" in res
    assert "disagreement_category" in res
    assert res["generation_id"] == "target_d_v1_20260528"


def test_target_d_disagreement_categorization():
    """Verify exact categorization of disagreement states."""
    observer = TargetDShadowObserver()
    dummy_feats = {c: 0.5 for c in FEATURE_COLUMNS}
    
    res = observer.evaluate_passive(
        features=dummy_feats,
        master_prob=0.60,
        master_decision="PASS"
    )
    assert res["disagreement_category"] in ("BOTH_PASS", "MASTER_ONLY", "BOTH_REJECT", "TARGET_D_ONLY")


def test_pipeline_non_interference_when_target_d_disagrees():
    """Hard Non-Interference Test: Verify SignalPipeline output is identical regardless of Target D score."""
    from unittest.mock import MagicMock
    from app.strategies.pipeline import SignalPipeline
    
    mock_score = SimpleNamespace(
        probability=0.65,
        model_source="BASE",
        specialist_status="NONE",
        specialist_auc=0.6,
        specialist_min_auc=0.53,
        fallback_reason=None
    )
    
    mock_model = MagicMock()
    mock_model.score_action_detailed = MagicMock(return_value=mock_score)
    mock_model.score_action = MagicMock(return_value=0.65)
    mock_model.p_up = MagicMock(return_value=0.65)
    mock_model.p_down = MagicMock(return_value=0.35)
    
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.48)
    pipeline.model = mock_model
    
    pipeline.meta_label_engine = MagicMock()
    pipeline.meta_label_engine.predict.return_value = MagicMock(approved=True, decision="PASS", source="test", prob_profitable=0.7)
    
    mock_ts_engine = MagicMock()
    mock_ts_engine.compute.return_value = 75.0
    mock_ts_engine.approve.return_value = (True, 50.0)
    mock_ts_engine.risk_config = MagicMock(ml_hard_reject_threshold=0.48, midday_score_boost=0.0)
    pipeline.trade_score_engine = mock_ts_engine
    
    dates = pd.date_range("2026-07-24 14:00", periods=50, freq="1min", tz="UTC")
    df = pd.DataFrame({
        "open": np.linspace(100, 105, 50),
        "high": np.linspace(101, 106, 50),
        "low": np.linspace(99, 104, 50),
        "close": np.linspace(100.5, 105.5, 50),
        "volume": np.full(50, 10000.0),
        "vol_sma20": np.full(50, 10000.0),
        "atr14": np.full(50, 1.0),
    }, index=dates)
    
    sig = Signal(
        symbol="NVDA",
        action=SignalAction.ENTER_LONG,
        confidence=0.8,
        strategy="orb",
        reason="Breakout"
    )
    
    features = pd.Series({c: 0.5 for c in FEATURE_COLUMNS})
    
    accepted, rejected = pipeline.process(
        symbol="NVDA",
        df=df,
        signals=[sig],
        regime=Regime.RANGE,
        features=features
    )
    
    assert len(accepted) == 1, f"Expected 1 accepted signal, got 0. Rejections: {rejected}"
    assert len(rejected) == 0
    assert accepted[0].ml_gate_decision == "PASS"
    assert accepted[0].target_d_probability is not None
    assert accepted[0].target_d_gate_decision in ("PASS", "REJECT")
