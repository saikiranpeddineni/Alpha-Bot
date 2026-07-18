from __future__ import annotations

import pytest
from app.strategies.adaptive import (
    get_adaptive_ml_threshold,
    get_adaptive_specialist_min_auc,
    get_adaptive_signal_score,
)


def test_get_adaptive_ml_threshold():
    # Base: 0.52
    base = 0.52

    # Case 1: Less than 10 trades -> returns base unchanged
    assert get_adaptive_ml_threshold(base, {"trades_count": 9, "shrunk_expectancy": 0.5}) == base

    # Case 2: High expectancy (+0.4) -> lower threshold by 0.02
    assert get_adaptive_ml_threshold(base, {"trades_count": 10, "shrunk_expectancy": 0.4}) == pytest.approx(0.50)

    # Case 3: Negative expectancy (-0.15) -> raise threshold by 0.03
    assert get_adaptive_ml_threshold(base, {"trades_count": 12, "shrunk_expectancy": -0.15}) == pytest.approx(0.55)

    # Boundary: min ML threshold is 0.45
    assert get_adaptive_ml_threshold(0.46, {"trades_count": 10, "shrunk_expectancy": 0.5}) == 0.45

    # Boundary: max ML threshold is 0.60
    assert get_adaptive_ml_threshold(0.59, {"trades_count": 10, "shrunk_expectancy": -0.3}) == 0.60


def test_get_adaptive_specialist_min_auc():
    # Case 1: Reversion strategy in RANGE -> lower required Specialist AUC to 0.50
    assert get_adaptive_specialist_min_auc("vwap_reversion", "RANGE", None) == 0.50
    assert get_adaptive_specialist_min_auc("rsi_reversion", "RANGE", 0.5) == 0.50

    # Case 2: Momentum strategy with weak relative strength -> raise required Specialist AUC to 0.56
    assert get_adaptive_specialist_min_auc("orb", "TREND_UP", 0.35) == 0.56
    assert get_adaptive_specialist_min_auc("momentum", "VOLATILE", 0.2) == 0.56

    # Case 3: Default fallback -> returns base 0.53
    assert get_adaptive_specialist_min_auc("orb", "TREND_UP", 0.60) == 0.53
    assert get_adaptive_specialist_min_auc("vwap_reversion", "TREND_DOWN", None) == 0.53


def test_get_adaptive_signal_score():
    base_score = 60.0

    # Case 1: Less than 10 trades -> returns base score unchanged
    assert get_adaptive_signal_score(base_score, {"trades_count": 5, "shrunk_expectancy": 0.5}) == base_score

    # Case 2: High expectancy (+0.3) -> scales score by 1.30
    assert get_adaptive_signal_score(base_score, {"trades_count": 10, "shrunk_expectancy": 0.3}) == pytest.approx(78.0)

    # Case 3: Negative expectancy (-0.2) -> scales score by 0.80
    assert get_adaptive_signal_score(base_score, {"trades_count": 10, "shrunk_expectancy": -0.2}) == pytest.approx(48.0)

    # Boundary: max multiplier is 1.5
    assert get_adaptive_signal_score(base_score, {"trades_count": 10, "shrunk_expectancy": 0.8}) == pytest.approx(90.0)

    # Boundary: min multiplier is 0.2
    assert get_adaptive_signal_score(base_score, {"trades_count": 10, "shrunk_expectancy": -0.95}) == pytest.approx(12.0)
