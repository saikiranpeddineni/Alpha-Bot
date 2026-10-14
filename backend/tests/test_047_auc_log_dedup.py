"""Tests for issue 047 — per-session specialist AUC log deduplication."""
from __future__ import annotations
import pytest
import joblib
import logging
from pathlib import Path
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from app.ai.model import SignalModel
from app.core.domain import SignalAction
from app.ai.features import FEATURE_COLUMNS
import pandas as pd


def _feature_version() -> str:
    import hashlib
    return hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]


def make_failing_specialist(tmp_path: Path, symbol: str) -> None:
    X = np.random.rand(50, len(FEATURE_COLUMNS))
    y = (X[:, 0] > 0.5).astype(int)
    clf = GradientBoostingClassifier(n_estimators=5).fit(X, y)
    artifact = {
        'long': clf,
        'short': clf,
        'auc': 0.48,  # below 0.52 min threshold
        'auc_long': 0.48,
        'auc_short': 0.48,
        'n_samples': 50,
        'n_positive': 25,
        'n_negative': 25,
        'trained_at': '2026-07-15T18:00:00+00:00',
        'feature_version': _feature_version(),
    }
    specs_dir = tmp_path / 'specialists'
    specs_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, specs_dir / f'{symbol.lower()}.pkl')


def test_auc_warning_logged_only_once_per_session(tmp_path, caplog):
    """VALIDATION FAILED logged at INFO first time, DEBUG on subsequent calls."""
    make_failing_specialist(tmp_path, 'SOFI')
    model = SignalModel(tmp_path)
    features = pd.Series(np.ones(len(FEATURE_COLUMNS)) * 0.5, index=FEATURE_COLUMNS)

    # Must target the 'alphadesk' logger specifically — applog routes through
    # logging.getLogger('alphadesk') and the root caplog handler alone does
    # not lower the alphadesk logger's effective level to DEBUG.
    with caplog.at_level(logging.DEBUG, logger='alphadesk'):
        result1 = model.score_action_detailed(features, SignalAction.ENTER_LONG, symbol='SOFI')
        result2 = model.score_action_detailed(features, SignalAction.ENTER_LONG, symbol='SOFI')

    assert result1.specialist_status == 'REJECTED_LOW_AUC'
    assert result2.specialist_status == 'REJECTED_LOW_AUC'

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO and 'SOFI' in r.message and 'VALIDATION FAILED' in r.message]
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG and 'SOFI' in r.message and 'suppressed' in r.message]
    assert len(info_msgs) == 1, f'Expected 1 INFO, got {len(info_msgs)}'
    assert len(debug_msgs) >= 1, f'Expected >= 1 DEBUG suppressed, got {len(debug_msgs)}'


def test_auc_warn_set_resets_on_reload(tmp_path, caplog):
    """After reload_specialists(), the dedup set resets so first call logs at INFO again.

    Note: the public reload API is reload_specialists() which also resets
    _auc_warned_symbols. There is no _load() method — that was an internal
    implementation detail that no longer exists as a named callable.
    """
    make_failing_specialist(tmp_path, 'MU')
    model = SignalModel(tmp_path)
    features = pd.Series(np.ones(len(FEATURE_COLUMNS)) * 0.5, index=FEATURE_COLUMNS)

    # Must target the 'alphadesk' logger specifically — see test above
    with caplog.at_level(logging.DEBUG, logger='alphadesk'):
        model.score_action_detailed(features, SignalAction.ENTER_LONG, symbol='MU')
        # MU is now in warned set
        assert 'MU' in model._auc_warned_symbols

        # Reload via the public API (reload_specialists resets _auc_warned_symbols)
        model.reload_specialists()
        assert 'MU' not in model._auc_warned_symbols  # reset

        # Next call should log at INFO again
        model.score_action_detailed(features, SignalAction.ENTER_LONG, symbol='MU')

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO and 'MU' in r.message and 'VALIDATION FAILED' in r.message]
    assert len(info_msgs) == 2  # once before reload, once after
