


"""Tests for specialist promotion safety: min_specialist_auc floor and side-preservation."""
from __future__ import annotations

def _mock_settings():
    from unittest.mock import MagicMock
    s = MagicMock()
    s.walk_forward_train_days = 15
    s.walk_forward_val_days = 5
    s.walk_forward_min_windows = 2
    r = MagicMock()
    r.min_specialist_auc = 0.50
    s.risk_config.return_value = r
    return s

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier

from app.ai.features import FEATURE_COLUMNS
from app.ai.trainer import _LegacyContinuousTrainer


def _feature_version() -> str:
    return hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]


def test_specialist_sub_floor_not_promoted(tmp_path: Path):
    """Candidate with AUC < min_specialist_auc (0.53) must NOT be promoted even if it beats a lower incumbent."""
    models_dir = tmp_path / "models"
    specialist_dir = models_dir / "specialists"
    specialist_dir.mkdir(parents=True, exist_ok=True)

    # Existing incumbent has low AUC
    incumbent_artifact = {
        "long": HistGradientBoostingClassifier().fit(np.zeros((10, len(FEATURE_COLUMNS))), [0]*5 + [1]*5),
        "short": HistGradientBoostingClassifier().fit(np.zeros((10, len(FEATURE_COLUMNS))), [0]*5 + [1]*5),
        "auc": 0.45,
        "auc_long": 0.45,
        "auc_short": 0.45,
        "feature_version": _feature_version(),
    }
    joblib.dump(incumbent_artifact, specialist_dir / "crwv.pkl")

    model = MagicMock()
    model._specialists = {}
    trainer = _LegacyContinuousTrainer(MagicMock(), model, models_dir, settings=_mock_settings())

    dates = pd.date_range("2026-01-01", periods=400, freq="1min", tz="UTC")
    df_fake = pd.DataFrame({
        "open": np.linspace(10, 15, 400),
        "high": np.linspace(10, 15, 400) + 0.5,
        "low": np.linspace(10, 15, 400) - 0.5,
        "close": np.linspace(10, 15, 400),
        "volume": 1000.0,
    }, index=dates)

    X_fake = pd.DataFrame(np.random.rand(400, len(FEATURE_COLUMNS)), columns=FEATURE_COLUMNS, index=dates)
    y_fake = pd.Series([0, 1] * 200, index=dates)
    w_fake = pd.Series(np.ones(400), index=dates)

    with patch("app.ai.features.build_training_set", return_value=(X_fake, y_fake, w_fake)):
        with patch("sklearn.metrics.roc_auc_score", return_value=0.4963):
            res = trainer._fit_specialist_sync("CRWV", df_fake)
            assert res["decision"] == "RETAINED_INCUMBENT"
            assert res["candidate_auc_long"] == 0.4963
            # Ensure disk artifact was NOT overwritten
            loaded = joblib.load(specialist_dir / "crwv.pkl")
            assert loaded["auc_long"] == 0.45


def test_specialist_side_preservation_on_promotion(tmp_path: Path):
    """When short improves and is promoted, incumbent long (0.58) must NOT be overwritten by candidate long (0.51)."""
    models_dir = tmp_path / "models"
    specialist_dir = models_dir / "specialists"
    specialist_dir.mkdir(parents=True, exist_ok=True)

    incumbent_long_clf = "INCUMBENT_LONG_MODEL"
    incumbent_short_clf = "INCUMBENT_SHORT_MODEL"

    incumbent_artifact = {
        "long": incumbent_long_clf,
        "short": incumbent_short_clf,
        "auc": 0.5846,
        "auc_long": 0.5846,
        "auc_short": 0.5008,
        "feature_version": _feature_version(),
    }
    joblib.dump(incumbent_artifact, specialist_dir / "baba.pkl")

    model = MagicMock()
    model._specialists = {"BABA": incumbent_artifact}
    trainer = _LegacyContinuousTrainer(MagicMock(), model, models_dir, settings=_mock_settings())

    dates = pd.date_range("2026-01-01", periods=400, freq="1min", tz="UTC")
    df_fake = pd.DataFrame({
        "open": np.linspace(10, 15, 400),
        "high": np.linspace(10, 15, 400) + 0.5,
        "low": np.linspace(10, 15, 400) - 0.5,
        "close": np.linspace(10, 15, 400),
        "volume": 1000.0,
    }, index=dates)

    X_fake = pd.DataFrame(np.random.rand(400, len(FEATURE_COLUMNS)), columns=FEATURE_COLUMNS, index=dates)
    y_fake = pd.Series([0, 1] * 200, index=dates)
    w_fake = pd.Series(np.ones(400), index=dates)

    auc_call_count = 0
    def mock_auc(*args, **kwargs):
        nonlocal auc_call_count
        auc_call_count += 1
        return 0.5155 if auc_call_count % 2 == 1 else 0.5400

    with patch("app.ai.features.build_training_set", return_value=(X_fake, y_fake, w_fake)):
        with patch("sklearn.metrics.roc_auc_score", side_effect=mock_auc):
            res = trainer._fit_specialist_sync("BABA", df_fake)
            assert res["decision"] == "PROMOTED"

            # Check promoted artifact on disk
            promoted = joblib.load(specialist_dir / "baba.pkl")
            # Long must be PRESERVED from incumbent
            assert promoted["auc_long"] == 0.5846
            assert promoted["long"] == incumbent_long_clf
            # Short must be UPDATED to new candidate
            assert promoted["auc_short"] == 0.5400
            assert promoted["short"] != incumbent_short_clf
            assert promoted["auc"] == 0.5846
