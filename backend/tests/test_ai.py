"""AI layer tests: features, regime detection, model gate, training."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.ai.features import FEATURE_COLUMNS, build_training_set, compute_features, latest_feature_row
from app.ai.model import SignalModel
from app.ai.regime import detect_regime
from app.core.domain import Regime, SignalAction
from app.data.synthetic import generate_day
from app.indicators.ta import enrich
from tests.conftest import make_bars


def test_feature_columns_complete():
    df = compute_features(make_bars(150))
    for col in FEATURE_COLUMNS:
        assert col in df.columns
    row = latest_feature_row(df)
    assert row is not None
    assert not row.isna().any()


def test_latest_feature_row_none_during_warmup():
    assert latest_feature_row(make_bars(5)) is None


def test_training_set_labels_are_forward_looking():
    df = make_bars(300, drift=0.001, vol=0.0005, seed=5)  # strong steady uptrend
    X, y = build_training_set(df, horizon_bars=15, threshold_pct=0.10)
    assert len(X) == len(y)
    assert len(X) > 200
    assert y.mean() > 0.5  # uptrend -> mostly positive labels


def test_heuristic_gate_direction(tmp_path):
    model = SignalModel(tmp_path / "models")
    assert not model.is_trained
    up = latest_feature_row(compute_features(make_bars(200, drift=0.0008, seed=2)))
    down = latest_feature_row(compute_features(make_bars(200, drift=-0.0008, seed=2)))
    p_up_trending_up = model.p_up(up)
    p_up_trending_down = model.p_up(down)
    assert p_up_trending_up > 0.5 > p_up_trending_down
    # score_action flips for shorts
    assert model.score_action(down, SignalAction.ENTER_SHORT) > 0.5
    assert model.score_action(None, SignalAction.ENTER_LONG) == 0.5


def test_live_model_has_no_backtest_start_time(tmp_path):
    """Live scoring must not depend on a backtest-only attribute being injected."""
    model = SignalModel(tmp_path / "models")
    assert model.backtest_start_time is None


def test_model_train_save_load(tmp_path):
    frames = {
        "SPY": generate_day("SPY", pd.Timestamp("2026-01-05").date()),
        "QQQ": generate_day("QQQ", pd.Timestamp("2026-01-05").date()),
    }
    # Extend with a second day for enough samples
    for sym in list(frames):
        frames[sym] = pd.concat([frames[sym], generate_day(sym, pd.Timestamp("2026-01-06").date())])
    model = SignalModel(tmp_path / "models")
    info = model.train(frames, horizon_bars=10, threshold_pct=0.05)
    assert info["trained"]
    assert info["samples"] > 500
    assert 0.0 <= info["validation_accuracy"] <= 1.0
    reloaded = SignalModel(tmp_path / "models")
    assert reloaded.is_trained
    row = latest_feature_row(compute_features(frames["SPY"]))
    p = reloaded.p_up(row)
    assert 0.0 <= p <= 1.0


def test_regime_detection_classes():
    trend = enrich(make_bars(400, drift=0.0006, vol=0.0004, seed=9))
    regime, details = detect_regime(trend)
    assert regime in (Regime.TREND_UP, Regime.VOLATILE)
    assert details["er"] is not None

    # Fast oscillation (~5-bar period) so no 20-bar window looks like a trend
    chop_prices = 100 + np.sin(np.arange(400) * 1.3) * 0.3
    chop = make_bars(400, seed=10)
    chop["close"] = chop_prices
    chop["open"] = np.concatenate([[100.0], chop_prices[:-1]])
    chop["high"] = np.maximum(chop["open"], chop["close"]) + 0.05
    chop["low"] = np.minimum(chop["open"], chop["close"]) - 0.05
    regime2, _ = detect_regime(enrich(chop))
    assert regime2 in (Regime.RANGE, Regime.VOLATILE)

    warmup, _ = detect_regime(enrich(make_bars(30)))
    assert warmup == Regime.RANGE





def test_adaptive_model_tuning(tmp_path):
    frames = {
        "SPY": generate_day("SPY", pd.Timestamp("2026-01-05").date()),
        "QQQ": generate_day("QQQ", pd.Timestamp("2026-01-05").date()),
    }
    for sym in list(frames):
        frames[sym] = pd.concat([frames[sym], generate_day(sym, pd.Timestamp("2026-01-06").date())])
    
    model = SignalModel(tmp_path / "models")
    
    model.train(frames, horizon_bars=10, threshold_pct=0.05)
    assert model.is_trained
    assert model._adaptive_long is None
    
    info = model.train_adaptive(frames, horizon_bars=10, threshold_pct=0.05, save_to_disk=False)
    assert info["status"] == "success"
    assert model._adaptive_long is not None
    assert model._adaptive_short is not None
    
    row = latest_feature_row(compute_features(frames["SPY"]))
    p = model.p_up(row)
    assert 0.0 <= p <= 1.0


def test_gate_report_with_none_realized_r():
    from app.ai.gate_report import build_gate_value_report
    shadow_signals = [
        {
            "final_decision": "accepted",
            "replayed": True,
            "outcomes": {
                "15m": {
                    "fwd_return": None,
                    "realized_r": None,
                    "stop_is_synthetic": True,
                }
            }
        },
        {
            "final_decision": "rejected",
            "rejection_stage": "ml_gate",
            "replayed": True,
            "outcomes": {
                "15m": {
                    "fwd_return": 0.05,
                    "realized_r": 1.2,
                    "stop_is_synthetic": False,
                }
            }
        }
    ]
    report = build_gate_value_report(shadow_signals, horizon="15m")
    assert report["horizon"] == "15m"
    assert report["all_candidates"]["candidates"] == 2
    # The first candidate had None outcomes -> mapped to 0.0
    assert report["all_candidates"]["expectancy_r"] == pytest.approx(0.6)  # (0.0 + 1.2) / 2 = 0.6

