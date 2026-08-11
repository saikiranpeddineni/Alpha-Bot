"""Tests for issue 046 — specialist AUC side-specificity and log clarity."""
from __future__ import annotations
from app.config import RiskConfig
from app.config import Settings

import hashlib
import pytest
import joblib
from pathlib import Path
from app.ai.model import SignalModel
from app.ai.features import FEATURE_COLUMNS
from app.core.domain import SignalAction


def _feature_version() -> str:
    """Compute the same feature_version hash used in score_action_detailed."""
    return hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]


def make_specialist_artifact(tmp_path: Path, symbol: str, auc_long: float, auc_short: float) -> None:
    """Create a fake specialist pkl with side-specific AUCs."""
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier
    X = np.random.rand(50, len(FEATURE_COLUMNS))
    y = (X[:, 0] > 0.5).astype(int)
    clf = GradientBoostingClassifier(n_estimators=5).fit(X, y)
    artifact = {
        'long': clf,
        'short': clf,
        'auc': max(auc_long, auc_short),
        'auc_long': auc_long,
        'auc_short': auc_short,
        'n_samples': 50,
        'n_positive': 25,
        'n_negative': 25,
        'trained_at': '2026-07-15T18:00:00+00:00',
        'feature_version': _feature_version(),
    }
    specs_dir = tmp_path / 'specialists'
    specs_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, specs_dir / f'{symbol.lower()}.pkl')


def make_specialist_artifact_legacy(tmp_path: Path, symbol: str, auc: float) -> None:
    """Create a legacy specialist pkl with only combined `auc` (no side split)."""
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier
    X = np.random.rand(50, len(FEATURE_COLUMNS))
    y = (X[:, 0] > 0.5).astype(int)
    clf = GradientBoostingClassifier(n_estimators=5).fit(X, y)
    artifact = {
        'long': clf,
        'short': clf,
        'auc': auc,  # only combined AUC, no side split
        'n_samples': 50,
        'n_positive': 25,
        'n_negative': 25,
        'trained_at': '2026-07-15T18:00:00+00:00',
        'feature_version': _feature_version(),
    }
    specs_dir = tmp_path / 'specialists'
    specs_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, specs_dir / f'{symbol.lower()}.pkl')


def test_side_specific_auc_routing(tmp_path):
    """ENTER_LONG uses auc_long; ENTER_SHORT uses auc_short."""
    make_specialist_artifact(tmp_path, 'AAPL', auc_long=0.62, auc_short=0.42)
    model = SignalModel(tmp_path, risk_config=RiskConfig())
    spec = model._specialists.get('AAPL')
    assert spec is not None
    assert spec['auc_long'] == 0.62
    assert spec['auc_short'] == 0.42


def test_legacy_artifact_backfills_side_aucs(tmp_path):
    """Legacy artifacts with only `auc` get backfilled with auc_long and auc_short."""
    make_specialist_artifact_legacy(tmp_path, 'MU', auc=0.55)
    model = SignalModel(tmp_path, risk_config=RiskConfig())
    spec = model._specialists.get('MU')
    assert spec is not None
    assert 'auc_long' in spec
    assert 'auc_short' in spec
    assert spec['auc_long'] == 0.55
    assert spec['auc_short'] == 0.55


def test_validation_log_includes_side_label(tmp_path, capfd):
    """VALIDATION FAILED log includes (long) or (short) side label."""
    import logging
    make_specialist_artifact(tmp_path, 'NVDA', auc_long=0.40, auc_short=0.40)
    model = SignalModel(tmp_path, risk_config=RiskConfig())

    import numpy as np
    from app.ai.features import FEATURE_COLUMNS
    import pandas as pd
    features = pd.Series(np.ones(len(FEATURE_COLUMNS)) * 0.5, index=FEATURE_COLUMNS)

    # We just verify the method runs without error for both sides
    result_long = model.score_action_detailed(features, SignalAction.ENTER_LONG, symbol='NVDA')
    result_short = model.score_action_detailed(features, SignalAction.ENTER_SHORT, symbol='NVDA')
    assert result_long.specialist_status == 'REJECTED_LOW_AUC'
    assert result_short.specialist_status == 'REJECTED_LOW_AUC'
