import pytest
from app.core.domain import Regime
from app.risk.market_regime import MarketRiskState
from app.core.regime_matrix import RegimeMatrix, UnifiedRegimeLabel


def test_regime_matrix_market_stress():
    posture = RegimeMatrix.evaluate(MarketRiskState.STRESS, Regime.TREND_UP)
    assert posture.regime_label == UnifiedRegimeLabel.MARKET_STRESS
    assert not posture.allow_long
    assert not posture.allow_short
    assert posture.risk_scalar == 0.0

    posture_panic = RegimeMatrix.evaluate(MarketRiskState.PANIC, Regime.RANGE)
    assert posture_panic.regime_label == UnifiedRegimeLabel.MARKET_STRESS
    assert posture_panic.risk_scalar == 0.0

    posture_unknown = RegimeMatrix.evaluate(MarketRiskState.UNKNOWN, Regime.VOLATILE)
    assert posture_unknown.regime_label == UnifiedRegimeLabel.MARKET_STRESS
    assert posture_unknown.risk_scalar == 0.0


def test_regime_matrix_high_volatility():
    posture = RegimeMatrix.evaluate(MarketRiskState.NORMAL, Regime.VOLATILE)
    assert posture.regime_label == UnifiedRegimeLabel.HIGH_VOLATILITY
    assert posture.allow_long
    assert posture.allow_short
    assert posture.risk_scalar == 0.50

    posture_atr = RegimeMatrix.evaluate(MarketRiskState.NORMAL, Regime.RANGE, atr_percentile=0.90)
    assert posture_atr.regime_label == UnifiedRegimeLabel.HIGH_VOLATILITY
    assert posture_atr.risk_scalar == 0.50


def test_regime_matrix_low_volatility():
    posture = RegimeMatrix.evaluate(MarketRiskState.NORMAL, Regime.RANGE, atr_percentile=0.15)
    assert posture.regime_label == UnifiedRegimeLabel.LOW_VOLATILITY
    assert posture.allow_long
    assert not posture.allow_short
    assert posture.risk_scalar == 0.75


def test_regime_matrix_trending_bullish():
    posture = RegimeMatrix.evaluate(MarketRiskState.NORMAL, Regime.TREND_UP)
    assert posture.regime_label == UnifiedRegimeLabel.TRENDING_BULLISH
    assert posture.allow_long
    assert not posture.allow_short
    assert posture.risk_scalar == 1.0


def test_regime_matrix_trending_bearish():
    posture = RegimeMatrix.evaluate(MarketRiskState.NORMAL, Regime.TREND_DOWN)
    assert posture.regime_label == UnifiedRegimeLabel.TRENDING_BEARISH
    assert not posture.allow_long
    assert posture.allow_short
    assert posture.risk_scalar == 1.0


def test_regime_matrix_sideways_range():
    posture = RegimeMatrix.evaluate(MarketRiskState.NORMAL, Regime.RANGE)
    assert posture.regime_label == UnifiedRegimeLabel.SIDEWAYS_RANGE
    assert posture.allow_long
    assert posture.allow_short
    assert posture.risk_scalar == 0.75
