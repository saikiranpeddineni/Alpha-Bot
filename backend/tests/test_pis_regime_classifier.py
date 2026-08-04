"""Unit and Integration Tests for PIS Market Regime Classifier (Quadrant 1)."""

from collections import deque
from datetime import datetime, timezone

import pytest

from app.pis.models import MarketRegime, PISConfig, PositionObservation, TradeHealth
from app.pis.regime_classifier import MarketRegimeClassifier


def _make_obs(price: float, high: float, low: float, vol: float = 1000.0, atr: float = 1.0, side: float = 1.0):
    return PositionObservation(
        timestamp=datetime.now(timezone.utc),
        symbol="TEST",
        side=side,
        qty=100,
        entry_price=100.0,
        current_price=price,
        high=high,
        low=low,
        close=price,
        volume=vol,
        atr=atr,
        initial_stop=98.0 if side > 0 else 102.0,
        risk_per_share=2.0,
        r_multiple=(price - 100.0) / 2.0 * side,
    )


def test_regime_classifier_trend_runner():
    classifier = MarketRegimeClassifier()
    health = TradeHealth(overall=0.85, trend_score=0.85, momentum_score=0.80, volatility_score=0.80, volume_score=0.90)

    history = deque(
        [
            _make_obs(100.0, 100.5, 99.8, vol=1000),
            _make_obs(101.0, 101.5, 100.5, vol=2000),
            _make_obs(102.5, 103.0, 101.8, vol=3500),
            _make_obs(104.0, 104.5, 103.0, vol=4000),
        ]
    )

    regime, conf, summary = classifier.classify_regime(history[-1], history, health)
    assert regime == MarketRegime.TREND_RUNNER
    assert conf >= 0.70
    assert "Trend" in summary or "Explosive" in summary


def test_regime_classifier_range_bound():
    classifier = MarketRegimeClassifier()
    health = TradeHealth(overall=0.55, trend_score=0.45, momentum_score=0.50, volatility_score=0.75, volume_score=0.50)

    history = deque(
        [
            _make_obs(100.0, 100.5, 99.5, vol=1000),
            _make_obs(100.4, 100.6, 99.8, vol=1100),
            _make_obs(99.8, 100.2, 99.6, vol=900),
            _make_obs(100.1, 100.5, 99.7, vol=1050),
        ]
    )

    regime, conf, summary = classifier.classify_regime(history[-1], history, health)
    assert regime == MarketRegime.RANGE_BOUND
    assert conf >= 0.60
    assert "Range" in summary or "Mean Reversion" in summary


def test_regime_classifier_choppy_defensive():
    classifier = MarketRegimeClassifier()
    health = TradeHealth(overall=0.25, trend_score=0.20, momentum_score=0.25, volatility_score=0.50, volume_score=0.25)

    history = deque(
        [
            _make_obs(100.0, 100.2, 99.5, vol=500),
            _make_obs(99.0, 99.5, 98.8, vol=3000),
            _make_obs(98.5, 99.0, 98.2, vol=4500),
        ]
    )

    regime, conf, summary = classifier.classify_regime(history[-1], history, health)
    assert regime == MarketRegime.CHOPPY_DEFENSIVE
    assert "Choppy" in summary or "Defensive" in summary
