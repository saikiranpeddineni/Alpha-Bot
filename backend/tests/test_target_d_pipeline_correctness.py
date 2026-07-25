"""Unit tests for Target D Shadow Observer and Signal Pipeline Target/Stop Directionality."""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock

from app.ai.target_d_observer import TargetDShadowObserver
from app.core.domain import Signal, SignalAction, Regime
from app.strategies.pipeline import SignalPipeline


def test_target_d_observer_fail_open_on_missing_features():
    observer = TargetDShadowObserver()
    # Passing incomplete/None feature dict returns UNAVAILABLE
    res = observer.evaluate_passive(None, master_prob=0.60, master_decision="PASS")
    assert res["target_d_decision"] == "UNAVAILABLE"
    assert res["target_d_probability"] is None
    assert res["disagreement_category"] == "UNAVAILABLE"


def test_target_d_observer_fail_open_on_nan_feature_values():
    observer = TargetDShadowObserver()
    incomplete_features = {
        "ret_1": 0.01,
        "ret_5": np.nan,  # Invalid NaN
        "ret_15": 0.02,
    }
    res = observer.evaluate_passive(incomplete_features, master_prob=0.60, master_decision="PASS")
    assert res["target_d_decision"] == "UNAVAILABLE"
    assert res["target_d_probability"] is None


def test_signal_pipeline_short_excursion_fallback_targets():
    mock_model = MagicMock()
    mock_model.score_action.return_value = 0.75
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.50)
    meta_res = MagicMock()
    meta_res.approved = True
    meta_res.decision = "PASS"
    meta_res.source = "MOCK"
    pipeline.meta_label_engine.predict = MagicMock(return_value=meta_res)

    # Signal without explicit take_profit or stop_loss
    short_sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_SHORT,
        strategy="momentum",
        price=150.0,
        stop_loss=None,
        take_profit=None,
        confidence=0.8,
        reason="test_short",
        regime=Regime.TREND_DOWN,
    )

    times = pd.date_range("2026-07-24 09:30:00", periods=100, freq="1min", tz="UTC")
    prices = [150.0 + np.sin(i / 5.0) for i in range(100)]
    vols = [1000.0 + i * 10 for i in range(100)]
    df = pd.DataFrame({
        "open": prices,
        "high": [p + 0.5 for p in prices],
        "low": [p - 0.5 for p in prices],
        "close": prices,
        "volume": vols,
    }, index=times)
    accepted, rejected = pipeline.process("AAPL", df, [short_sig], Regime.TREND_DOWN)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    sig = accepted[0]

    # For a SHORT signal, take_profit must be BELOW entry (150.0) and stop_loss ABOVE entry (150.0)
    assert sig.take_profit is not None and sig.take_profit < 150.0, f"Expected TP < 150.0, got {sig.take_profit}"
    assert sig.stop_loss is not None and sig.stop_loss > 150.0, f"Expected SL > 150.0, got {sig.stop_loss}"
