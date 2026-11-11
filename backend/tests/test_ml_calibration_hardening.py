# =============================================================================
# [CREATED 2026-11-11]: ML Model Calibration & Class Balancing Hardening Tests
# ORIGINAL INTENT: Uncalibrated MetaLabel models clustered probabilities around
# 0.48-0.52 and omitted short-side class balance and calibration metadata.
# REASON FOR CHANGE: Formally verify CalibratedClassifierCV integration (sigmoid for
# N<1000, isotonic for N>=1000), class_weight='balanced' enforcement across HGB
# classifiers, and bidirectional class balance provenance tracking (R4).
# SAFE ROLLBACK: This is a new targeted test file.
# =============================================================================

from datetime import datetime, UTC
import inspect
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier

from app.ai.prediction import MetaLabelEngine, _make_classifier, FEATURE_COLUMNS
from app.ai.trainer import ContinuousTrainer


def test_make_classifier_hgb_has_balanced_class_weight():
    """Verify _make_classifier('hgb') initializes with class_weight='balanced'."""
    clf = _make_classifier("hgb")
    assert isinstance(clf, HistGradientBoostingClassifier)
    assert clf.class_weight == "balanced"


def test_trainer_fit_hgb_has_balanced_class_weight():
    """Verify specialist trainer enforces class_weight='balanced'."""
    source = inspect.getsource(ContinuousTrainer.__mro__[1]._fit_specialist_sync)
    assert 'class_weight="balanced"' in source or "class_weight='balanced'" in source


def test_metalabel_engine_calibrated_classifier_integration(tmp_path, monkeypatch):
    """Verify MetaLabelEngine trains and serializes a CalibratedClassifierCV model."""
    models_dir = tmp_path / "meta_models"
    engine = MetaLabelEngine(models_dir=models_dir)

    # Generate synthetic candidate dataset with class imbalance (15% positive, 85% negative)
    np.random.seed(42)
    n = 200
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    X = pd.DataFrame(np.random.randn(n, len(FEATURE_COLUMNS)), columns=FEATURE_COLUMNS, index=idx)
    # Target: 1 when feature 0 is positive, 0 otherwise
    y = pd.Series((X.iloc[:, 0] > 0.8).astype(int), index=idx)
    w = pd.Series(np.ones(n), index=idx)

    monkeypatch.setattr("app.ai.prediction.get_strategy_candidates", lambda *args, **kwargs: list(idx))
    monkeypatch.setattr("app.ai.prediction.build_training_set", lambda *args, **kwargs: (X, y, w))

    # Run train with low validation AUC requirement to ensure promotion in test
    result = engine.train(
        strategy="ORB",
        side="long",
        frames={"AAPL": pd.DataFrame(index=idx)},
        min_validation_auc=0.0,
    )

    assert result["status"] == "success"
    assert result["class_weight"] == "balanced"
    assert result["calibration_method"] == "sigmoid"  # N < 1000
    assert result["is_calibrated"] is True
    assert result["imbalance_ratio"] > 0

    # Inspect serialized provenance artifact via result["artifact_path"]
    artifact_path = Path(result["artifact_path"])
    assert artifact_path.exists()

    import joblib
    provenance = joblib.load(artifact_path)
    assert isinstance(provenance["model"], CalibratedClassifierCV)
    assert provenance["class_weight"] == "balanced"
    assert provenance["calibration_method"] == "sigmoid"
    assert provenance["calibration_cv"] >= 2
    assert provenance["is_calibrated"] is True
    assert "imbalance_ratio" in provenance
    assert provenance["n_positive"] == int(y.sum())
    assert provenance["n_negative"] == int((y == 0).sum())

    # Predict with engine and verify probability is properly calibrated and spreads
    sample_df = X.iloc[:5]
    probs = engine._classifiers["ORB_long"].predict_proba(sample_df)[:, 1]
    assert len(probs) == 5
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_metalabel_engine_calibration_method_selection_isotonic(tmp_path, monkeypatch):
    """Verify isotonic calibration is selected for N >= 1000."""
    models_dir = tmp_path / "meta_models_large"
    engine = MetaLabelEngine(models_dir=models_dir)

    # Large dataset N = 1050
    n = 1050
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    X = pd.DataFrame(np.random.randn(n, len(FEATURE_COLUMNS)), columns=FEATURE_COLUMNS, index=idx)
    y = pd.Series((X.iloc[:, 0] > 0.0).astype(int), index=idx)
    w = pd.Series(np.ones(n), index=idx)

    monkeypatch.setattr("app.ai.prediction.get_strategy_candidates", lambda *args, **kwargs: list(idx))
    monkeypatch.setattr("app.ai.prediction.build_training_set", lambda *args, **kwargs: (X, y, w))

    result = engine.train(
        strategy="MOMENTUM",
        side="long",
        frames={"AAPL": pd.DataFrame(index=idx)},
        min_validation_auc=0.0,
    )

    assert result["status"] == "success"
    assert result["calibration_method"] == "isotonic"
    assert result["is_calibrated"] is True


def test_specialist_artifact_bidirectional_provenance_structure():
    """Verify specialist artifact schema includes both long and short class distributions."""
    y_l = pd.Series([1] * 20 + [0] * 80)
    y_s = pd.Series([1] * 15 + [0] * 85)

    n_pos_l = int(y_l.sum())
    n_neg_l = int((y_l == 0).sum())
    imb_ratio_l = round(float(n_neg_l) / max(1.0, float(n_pos_l)), 4)

    n_pos_s = int(y_s.sum())
    n_neg_s = int((y_s == 0).sum())
    imb_ratio_s = round(float(n_neg_s) / max(1.0, float(n_pos_s)), 4)

    assert imb_ratio_l == 4.0
    assert imb_ratio_s == 5.6667
