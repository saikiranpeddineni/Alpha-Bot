"""Unit tests for Unified 6-Regime RegimeMatrix wiring and dynamic risk allocation."""
from __future__ import annotations

from datetime import datetime, timezone
import pandas as pd
import pytest

from app.core.domain import Regime, Signal, SignalAction
from app.core.regime_matrix import RegimeMatrix, RegimePosture, UnifiedRegimeLabel
from app.portfolio.performance import StrategyPerformanceTracker, StrategyStats
from app.risk.manager import AccountContext, MarketClock, RiskConfig, RiskManager
from app.risk.market_regime import MarketRiskState
from app.strategies.pipeline import SignalPipeline


def _make_sample_df(n_bars: int = 50, price: float = 100.0) -> pd.DataFrame:
    """Construct sample 1Min bar dataframe with required columns."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    times = pd.date_range("2026-01-06 10:00:00", periods=n_bars, freq="1min", tz=et)
    df = pd.DataFrame(
        {
            "open": [price] * n_bars,
            "high": [price + 1.0] * n_bars,
            "low": [price - 1.0] * n_bars,
            "close": [price + 0.5] * n_bars,
            "volume": [1000.0] * n_bars,
            "atr14": [1.5] * n_bars,
            "vol_sma20": [1000.0] * n_bars,
        },
        index=times,
    )
    return df


class DummyModel:
    """Minimal dummy model for SignalPipeline."""
    def score_action(self, features, action):
        return 0.65


def test_regime_matrix_blocks_long_in_trending_bearish():
    """In TRENDING_BEARISH, RegimeMatrix must block long entries and allow shorts."""
    pipeline = SignalPipeline(model=DummyModel(), ml_threshold=0.0)
    df = _make_sample_df(30)
    sig_long = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=0.8, strategy="orb", reason="test_long", price=100.0, stop_loss=98.0, take_profit=105.0)
    sig_short = Signal(symbol="AAPL", action=SignalAction.ENTER_SHORT, confidence=0.8, strategy="orb", reason="test_short", price=100.0, stop_loss=102.0, take_profit=95.0)

    posture = RegimePosture(
        regime_label=UnifiedRegimeLabel.TRENDING_BEARISH,
        allow_long=False,
        allow_short=True,
        risk_scalar=1.0,
        confidence=0.90,
        rationale="Confirmed bearish trend",
    )

    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig_long, sig_short],
        regime=Regime.TREND_DOWN,
        posture=posture,
    )

    # Long must be rejected
    rejected_longs = [s for s, r in rejected if s.action == SignalAction.ENTER_LONG]
    assert len(rejected_longs) == 1
    assert "TRENDING_BEARISH" in rejected[0][1]

    # Short should survive regime filter
    assert any(s.action == SignalAction.ENTER_SHORT for s in accepted)


def test_regime_matrix_blocks_short_in_trending_bullish():
    """In TRENDING_BULLISH, RegimeMatrix must block short entries and allow longs."""
    pipeline = SignalPipeline(model=DummyModel(), ml_threshold=0.0)
    df = _make_sample_df(30)
    sig_long = Signal(symbol="NVDA", action=SignalAction.ENTER_LONG, confidence=0.8, strategy="orb", reason="test_long", price=100.0, stop_loss=98.0, take_profit=105.0)
    sig_short = Signal(symbol="NVDA", action=SignalAction.ENTER_SHORT, confidence=0.8, strategy="orb", reason="test_short", price=100.0, stop_loss=102.0, take_profit=95.0)

    posture = RegimePosture(
        regime_label=UnifiedRegimeLabel.TRENDING_BULLISH,
        allow_long=True,
        allow_short=False,
        risk_scalar=1.0,
        confidence=0.90,
        rationale="Confirmed bullish trend",
    )

    accepted, rejected = pipeline.process(
        symbol="NVDA",
        df=df,
        signals=[sig_long, sig_short],
        regime=Regime.TREND_UP,
        posture=posture,
    )

    rejected_shorts = [s for s, r in rejected if s.action == SignalAction.ENTER_SHORT]
    assert len(rejected_shorts) == 1
    assert "TRENDING_BULLISH" in rejected[0][1]
    assert any(s.action == SignalAction.ENTER_LONG for s in accepted)


def test_regime_matrix_freezes_all_entries_under_market_stress():
    """Under MARKET_STRESS, all entries are frozen; protective exits pass."""
    pipeline = SignalPipeline(model=DummyModel(), ml_threshold=0.0)
    df = _make_sample_df(30)
    sig_long = Signal(symbol="MSFT", action=SignalAction.ENTER_LONG, confidence=0.8, strategy="orb", reason="test_long", price=100.0, stop_loss=98.0, take_profit=105.0)
    sig_exit = Signal(symbol="MSFT", action=SignalAction.EXIT, confidence=1.0, strategy="orb", reason="test_exit", price=100.0)

    posture = RegimePosture(
        regime_label=UnifiedRegimeLabel.MARKET_STRESS,
        allow_long=False,
        allow_short=False,
        risk_scalar=0.0,
        confidence=0.95,
        rationale="Macro stress freeze",
    )

    accepted, rejected = pipeline.process(
        symbol="MSFT",
        df=df,
        signals=[sig_long, sig_exit],
        regime=Regime.VOLATILE,
        posture=posture,
    )

    # Entry rejected
    assert any(s.action == SignalAction.ENTER_LONG for s, _ in rejected)
    # Exit accepted
    assert any(s.action == SignalAction.EXIT for s in accepted)


def test_regime_matrix_blocks_mean_reversion_in_low_volatility():
    """In LOW_VOLATILITY, mean-reversion is suppressed while breakout is allowed."""
    pipeline = SignalPipeline(model=DummyModel(), ml_threshold=0.0)
    df = _make_sample_df(30)
    sig_vwap = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=0.8, strategy="vwap_reversion", reason="test_vwap", price=100.0, stop_loss=98.0, take_profit=104.0)
    sig_orb = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=0.8, strategy="orb", reason="test_orb", price=100.0, stop_loss=98.0, take_profit=105.0)

    posture = RegimePosture(
        regime_label=UnifiedRegimeLabel.LOW_VOLATILITY,
        allow_long=True,
        allow_short=False,
        risk_scalar=0.75,
        confidence=0.80,
        rationale="Low vol compression",
    )

    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig_vwap, sig_orb],
        regime=Regime.RANGE,
        posture=posture,
    )

    rejected_strats = [s.strategy for s, r in rejected]
    assert "vwap_reversion" in rejected_strats
    assert any(s.strategy == "orb" for s in accepted)


def test_risk_manager_momentum_50_pct_cap():
    """Momentum must remain capped at 0.50 until rolling expectancy is positive."""
    cfg = RiskConfig()
    rm = RiskManager(config=cfg, clock=MarketClock())
    tracker = StrategyPerformanceTracker()
    rm.perf_tracker = tracker

    # Cold start / 0 trades -> strictly capped at 0.50
    mult_cold = rm.get_strategy_risk_multiplier("momentum")
    assert mult_cold == 0.50

    # Negative expectancy -> still capped at 0.50
    stats = tracker.get_stats("momentum")
    stats.trades = 10
    stats._recent_r.extend([-1.0, -0.5, 0.2, -1.0, -0.8, -0.5, 0.1, -1.0, -0.5, -0.2])
    assert stats.rolling_expectancy_r < 0
    mult_neg = rm.get_strategy_risk_multiplier("momentum")
    assert mult_neg == 0.50

    # Positive expectancy (> 0) -> dynamically scales up above 0.50
    stats._recent_r.clear()
    stats._recent_r.extend([1.5, 2.0, 1.0, -0.5, 2.5, 1.2, 0.8, -1.0, 1.5, 2.0])
    assert stats.rolling_expectancy_r > 0
    mult_pos = rm.get_strategy_risk_multiplier("momentum")
    assert mult_pos > 0.50
    assert mult_pos <= 1.0


def test_scanner_sector_health_breakdown_penalty():
    """MarketScanner must penalize tech_score by 50% for symbols in breaking down sectors."""
    from app.core.scanner import MarketScanner
    candidates = [
        {"symbol": "NVDA", "rvol": 2.5, "gap_pct": 2.5, "atr_pct": 3.0, "mom3": 4.0},  # Sector ETF: SOXX
        {"symbol": "LOW", "rvol": 1.0, "gap_pct": 1.0, "atr_pct": 1.0, "mom3": 1.0},   # Lower baseline
    ]

    # 1. Scored with healthy sector
    scored_healthy = MarketScanner._stage2_technical_score(
        [dict(c) for c in candidates],
        sector_health={"SOXX": {"return_pct": 1.5, "slope": 0.2, "is_breaking_down": False}},
    )
    score_healthy = scored_healthy[0]["tech_score"]

    # 2. Scored with breaking down sector
    scored_breakdown = MarketScanner._stage2_technical_score(
        [dict(c) for c in candidates],
        sector_health={"SOXX": {"return_pct": -1.8, "slope": -0.25, "is_breaking_down": True}},
    )
    score_breakdown = scored_breakdown[0]["tech_score"]

    assert scored_breakdown[0].get("sector_breakdown") is True
    assert score_breakdown == pytest.approx(round(score_healthy * 0.50, 4), rel=1e-3)
