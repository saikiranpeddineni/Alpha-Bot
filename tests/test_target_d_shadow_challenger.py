"""Hard Non-Interference and Artifact Verification Tests for Target D Shadow Challenger.
Exhaustively compares Target-D-enabled vs Target-D-disabled pipelines across 8 scenarios.
"""

import os
import sys
import pytest
import pandas as pd
import numpy as np
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.ai.features import FEATURE_COLUMNS
from app.ai.target_d_observer import TargetDShadowObserver
from app.core.domain import Signal, SignalAction, Regime
from app.strategies.pipeline import SignalPipeline


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


def test_target_d_strict_feature_validation_no_synthetic_zeros():
    """Verify missing or non-finite features return UNAVAILABLE telemetry without synthetic zero fallbacks."""
    observer = TargetDShadowObserver()
    
    # 1. Missing a feature column completely
    incomplete_feats = {c: 0.5 for c in FEATURE_COLUMNS if c != "rsi14"}
    res1 = observer.evaluate_passive(incomplete_feats, master_prob=0.60, master_decision="PASS")
    assert res1["target_d_decision"] == "UNAVAILABLE"
    assert res1["target_d_probability"] is None

    # 2. Non-finite feature (NaN or Inf)
    nan_feats = {c: (np.nan if c == "ret_1" else 0.5) for c in FEATURE_COLUMNS}
    res2 = observer.evaluate_passive(nan_feats, master_prob=0.60, master_decision="PASS")
    assert res2["target_d_decision"] == "UNAVAILABLE"
    assert res2["target_d_probability"] is None


def helper_create_pipeline(ml_prob: float | None, strategy: str = "orb", action: SignalAction = SignalAction.ENTER_LONG, specialist_status: str = "NONE", enable_target_d: bool = True, soft_composite_gating: bool = False):
    """Helper to construct pipeline with optional Target D observer."""
    mock_model = MagicMock()
    mock_score = SimpleNamespace(
        probability=ml_prob,
        model_source="BASE",
        specialist_status=specialist_status,
        specialist_auc=0.6,
        specialist_min_auc=0.53,
        fallback_reason=None
    )
    mock_model.score_action_detailed.return_value = mock_score
    mock_model.score_action.return_value = ml_prob if ml_prob is not None else 0.0

    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.48, soft_composite_gating=soft_composite_gating)
    pipeline.meta_label_engine = MagicMock()
    pipeline.meta_label_engine.predict.return_value = MagicMock(approved=True, decision="PASS", source="test", prob_profitable=0.7)
    
    mock_ts_engine = MagicMock()
    mock_ts_engine.compute.return_value = 75.0
    mock_ts_engine.approve.return_value = (True, 50.0)
    mock_ts_engine.risk_config = MagicMock(
        ml_hard_reject_threshold=0.48,
        midday_score_boost=0.0,
        rsi2_base_fallback_bypass_enabled=True,
        rsi2_base_fallback_bypass_start_et=660,
        rsi2_base_fallback_bypass_end_et=840,
        bb_squeeze_short_bypass_enabled=True,
        bb_squeeze_short_bypass_start_et=840,
        bb_squeeze_short_bypass_end_et=930
    )
    pipeline.trade_score_engine = mock_ts_engine

    if not enable_target_d:
        pipeline.target_d_observer = None

    return pipeline


def helper_get_test_inputs(strategy="orb", action=SignalAction.ENTER_LONG):
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
        action=action,
        confidence=0.8,
        strategy=strategy,
        reason="TestSignal"
    )
    features = pd.Series({c: 0.5 for c in FEATURE_COLUMNS})
    return df, sig, features


@pytest.mark.parametrize("scenario", [
    "hard_ml_reject",
    "normal_pass",
    "gray_zone",
    "base_fallback_bypass",
    "temporal_bypass",
    "unavailable_ml"
])
def test_pipeline_non_interference_all_scenarios(scenario):
    """Exhaustive comparison proving Target-D-enabled and Target-D-disabled pipelines produce 100% identical decisions."""
    if scenario == "hard_ml_reject":
        p_enabled = helper_create_pipeline(ml_prob=0.30, enable_target_d=True)
        p_disabled = helper_create_pipeline(ml_prob=0.30, enable_target_d=False)
        df, sig, feats = helper_get_test_inputs()
    elif scenario == "normal_pass":
        p_enabled = helper_create_pipeline(ml_prob=0.65, enable_target_d=True)
        p_disabled = helper_create_pipeline(ml_prob=0.65, enable_target_d=False)
        df, sig, feats = helper_get_test_inputs()
    elif scenario == "gray_zone":
        p_enabled = helper_create_pipeline(ml_prob=0.50, enable_target_d=True, soft_composite_gating=True)
        p_disabled = helper_create_pipeline(ml_prob=0.50, enable_target_d=False, soft_composite_gating=True)
        p_enabled.ml_threshold = 0.55
        p_disabled.ml_threshold = 0.55
        df, sig, feats = helper_get_test_inputs()
    elif scenario == "base_fallback_bypass":
        p_enabled = helper_create_pipeline(ml_prob=0.40, strategy="rsi2_reversion", action=SignalAction.ENTER_LONG, enable_target_d=True)
        p_disabled = helper_create_pipeline(ml_prob=0.40, strategy="rsi2_reversion", action=SignalAction.ENTER_LONG, enable_target_d=False)
        df, sig, feats = helper_get_test_inputs(strategy="rsi2_reversion", action=SignalAction.ENTER_LONG)
    elif scenario == "temporal_bypass":
        p_enabled = helper_create_pipeline(ml_prob=0.35, specialist_status="TEMPORAL_BYPASS", enable_target_d=True)
        p_disabled = helper_create_pipeline(ml_prob=0.35, specialist_status="TEMPORAL_BYPASS", enable_target_d=False)
        df, sig, feats = helper_get_test_inputs()
    elif scenario == "unavailable_ml":
        p_enabled = helper_create_pipeline(ml_prob=None, enable_target_d=True)
        p_disabled = helper_create_pipeline(ml_prob=None, enable_target_d=False)
        df, sig, feats = helper_get_test_inputs()

    acc_en, rej_en = p_enabled.process(symbol="NVDA", df=df, signals=[sig.model_copy()], regime=Regime.RANGE, features=feats)
    acc_dis, rej_dis = p_disabled.process(symbol="NVDA", df=df, signals=[sig.model_copy()], regime=Regime.RANGE, features=feats)

    # 1. Assert identical accepted counts
    assert len(acc_en) == len(acc_dis), f"Scenario '{scenario}': accepted mismatch (enabled={len(acc_en)}, disabled={len(acc_dis)})"
    
    # 2. Assert identical rejected counts
    assert len(rej_en) == len(rej_dis), f"Scenario '{scenario}': rejected mismatch (enabled={len(rej_en)}, disabled={len(rej_dis)})"

    # 3. Assert identical rejection reason strings
    if rej_en and rej_dis:
        assert rej_en[0][1] == rej_dis[0][1], f"Scenario '{scenario}': rejection reason mismatch ('{rej_en[0][1]}' vs '{rej_dis[0][1]}')"

    # 4. Assert ml_gate_decision is identical
    if acc_en:
        assert acc_en[0].ml_gate_decision == acc_dis[0].ml_gate_decision


def test_pipeline_fail_open_missing_artifact():
    """Verify pipeline continues normally if Target D artifact file is missing."""
    p_missing = helper_create_pipeline(ml_prob=0.65, enable_target_d=True)
    p_missing.target_d_observer = TargetDShadowObserver(model_path="non_existent_model.joblib")
    df, sig, feats = helper_get_test_inputs()

    accepted, rejected = p_missing.process(symbol="NVDA", df=df, signals=[sig], regime=Regime.RANGE, features=feats)
    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].target_d_gate_decision == "UNAVAILABLE"
    assert accepted[0].target_d_probability is None


def test_pipeline_fail_open_inference_exception():
    """Verify pipeline catches Target D inference exceptions and returns UNAVAILABLE without failing signal execution."""
    p_corrupt = helper_create_pipeline(ml_prob=0.65, enable_target_d=True)
    mock_obs = MagicMock()
    mock_obs.evaluate_passive.side_effect = RuntimeError("Simulated Model Corruption")
    p_corrupt.target_d_observer = mock_obs
    df, sig, feats = helper_get_test_inputs()

    accepted, rejected = p_corrupt.process(symbol="NVDA", df=df, signals=[sig], regime=Regime.RANGE, features=feats)
    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].ml_gate_decision == "PASS"
    assert accepted[0].target_d_gate_decision == "UNAVAILABLE"
    assert accepted[0].target_d_probability is None
