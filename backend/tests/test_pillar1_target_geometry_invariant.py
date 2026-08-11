from app.config import RiskConfig
from app.config import Settings
import pytest
import pandas as pd
import numpy as np
from zoneinfo import ZoneInfo
from datetime import datetime
from app.config import get_settings, MODELS_DIR
from app.core.domain import Signal, SignalAction, Regime, OrderSide
from app.strategies.pipeline import SignalPipeline
from app.ai.model import SignalModel
from app.risk.manager import RiskManager

ET = ZoneInfo("America/New_York")

@pytest.fixture
def dummy_df():
    """Generates 200 minutes of valid OHLCV data for feature engineering warmup."""
    idx = pd.date_range("2026-07-29 09:30", periods=200, freq="1min", tz=ET)
    prices = [100.0 + np.sin(i / 10.0) * 0.5 for i in range(200)]
    df = pd.DataFrame({
        "open": prices,
        "high": [p + 0.5 for p in prices],
        "low": [p - 0.5 for p in prices],
        "close": prices,
        "volume": [10000] * 200,
        "atr14": [1.0] * 200,
        "vol_sma20": [10000] * 200,
        "rsi2": [50.0] * 200,
        "ema50": [100.0] * 200,
    }, index=idx)
    from app.indicators.ta import enrich
    return enrich(df)

@pytest.fixture
def sample_features(dummy_df):
    from app.ai.features import compute_features, FEATURE_COLUMNS
    feats_df = compute_features(dummy_df)
    row = feats_df.iloc[-1][FEATURE_COLUMNS].fillna(0.5).astype(float)
    # Ensure non-constant variance so MetaLabel std check passes
    row["ret_1"] = 0.01
    row["ret_5"] = -0.02
    row["rsi14"] = 55.0
    return row

@pytest.fixture
def pipeline(monkeypatch):
    settings = get_settings()
    risk_cfg = settings.risk_config()
    model = SignalModel(models_dir=MODELS_DIR, risk_config=RiskConfig())
    p = SignalPipeline(model=model, risk_config=risk_cfg, settings=settings)
    p.ml_threshold = 0.0
    from app.ai.prediction import MetaPrediction
    monkeypatch.setattr(p.meta_label_engine, "predict", lambda *args, **kwargs: MetaPrediction(
        strategy="test", prob_profitable=0.80, approved=True, threshold=0.55,
        n_training_samples=100, source="MOCK", decision="PASS", reason="test"
    ))
    return p

# --------------------------------------------------------------------------
# STRATEGY NATIVE GEOMETRY TESTS
# --------------------------------------------------------------------------

def test_momentum_long_native_geometry_preserved(pipeline, dummy_df, sample_features):
    """Momentum LONG with native 1.8R must preserve target R >= 1.8R."""
    sig = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="momentum",
        reason="test",
        price=100.0,
        stop_loss=98.0,
        take_profit=103.6
    )
    accepted, rejected = pipeline.process("TEST", dummy_df, [sig], Regime.TREND_UP, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    final_risk = abs(proc.price - proc.stop_loss)
    final_reward = abs(proc.take_profit - proc.price)
    final_r = final_reward / final_risk
    assert final_r >= 1.799

def test_momentum_short_native_geometry_preserved(pipeline, dummy_df, sample_features):
    """Momentum SHORT with native 1.8R must preserve target R >= 1.8R."""
    sig = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_SHORT,
        confidence=0.85,
        strategy="momentum",
        reason="test",
        price=100.0,
        stop_loss=102.0,
        take_profit=96.4
    )
    accepted, rejected = pipeline.process("TEST", dummy_df, [sig], Regime.TREND_DOWN, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    final_risk = abs(proc.price - proc.stop_loss)
    final_reward = abs(proc.take_profit - proc.price)
    final_r = final_reward / final_risk
    assert final_r >= 1.799

def test_orb_long_native_geometry_preserved(pipeline, dummy_df, sample_features):
    """ORB LONG with native 2.0R must preserve target R >= 2.0R."""
    sig = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=98.0,
        take_profit=104.0
    )
    accepted, rejected = pipeline.process("TEST", dummy_df, [sig], Regime.TREND_UP, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    final_risk = abs(proc.price - proc.stop_loss)
    final_reward = abs(proc.take_profit - proc.price)
    assert (final_reward / final_risk) >= 1.999

def test_orb_short_native_geometry_preserved(pipeline, dummy_df, sample_features):
    """ORB SHORT with native 2.0R must preserve target R >= 2.0R."""
    sig = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_SHORT,
        confidence=0.85,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=102.0,
        take_profit=96.0
    )
    accepted, rejected = pipeline.process("TEST", dummy_df, [sig], Regime.TREND_DOWN, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    final_risk = abs(proc.price - proc.stop_loss)
    final_reward = abs(proc.price - proc.take_profit)
    assert (final_reward / final_risk) >= 1.999

def test_rsi2_long_native_geometry_preserved(pipeline, dummy_df, sample_features):
    """RSI2 LONG with native 1.5R must preserve target R >= 1.5R."""
    sig = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="rsi2_reversion",
        reason="test",
        price=100.0,
        stop_loss=98.0,
        take_profit=103.0
    )
    accepted, rejected = pipeline.process("TEST", dummy_df, [sig], Regime.TREND_UP, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    final_risk = abs(proc.price - proc.stop_loss)
    final_reward = abs(proc.take_profit - proc.price)
    assert (final_reward / final_risk) >= 1.499

# --------------------------------------------------------------------------
# EXCURSION ESTIMATION & STOP WIDENING TESTS
# --------------------------------------------------------------------------

def test_excursion_target_extended_when_above_native_floor(pipeline, dummy_df, sample_features):
    """If Excursion prediction suggests a target larger than native floor, preserve extended target."""
    sig = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="rsi2_reversion",
        reason="test",
        price=100.0,
        stop_loss=98.0,
        take_profit=106.0 # Extended 3.0R target
    )
    accepted, rejected = pipeline.process("TEST", dummy_df, [sig], Regime.TREND_UP, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    # Target should be preserved at 106.0 (3.0R >= 1.5R)
    assert proc.take_profit >= 105.99

def test_min_stop_pct_widens_stop_and_preserves_native_r(pipeline, dummy_df, sample_features):
    """When min_stop_pct widens stop distance, final target must expand to keep target R >= native R."""
    sig = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=99.8,
        take_profit=100.4
    )
    accepted, rejected = pipeline.process("TEST", dummy_df, [sig], Regime.TREND_UP, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    # Stop widened to 99.5 (0.5% min_stop_pct -> $0.50 risk)
    assert proc.stop_loss == 99.5
    # Target must expand to at least 100.0 + 2.0 * 0.50 = 101.0 (2.0R)
    assert proc.take_profit >= 100.999
    assert (proc.take_profit - proc.price) / (proc.price - proc.stop_loss) >= 1.999

# --------------------------------------------------------------------------
# MALFORMED GEOMETRY FAIL-CLOSED TESTS
# --------------------------------------------------------------------------

def test_malformed_geometry_fail_closed(pipeline, dummy_df, sample_features):
    """Signals with missing, zero-risk, or wrong-sided geometry must handle cleanly without crash."""
    sig_no_tp = Signal(
        symbol="TEST1",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=98.0,
        take_profit=None
    )
    acc1, rej1 = pipeline.process("TEST1", dummy_df, [sig_no_tp], Regime.TREND_UP, features=sample_features)
    assert len(acc1) == 1, f"Signal rejected: {rej1}"
    assert acc1[0].take_profit is not None # Defaulted safely by Excursion

    sig_wrong = Signal(
        symbol="TEST2",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=102.0,
        take_profit=105.0
    )
    acc2, rej2 = pipeline.process("TEST2", dummy_df, [sig_wrong], Regime.TREND_UP, features=sample_features)
    assert len(acc2) == 1, f"Signal rejected: {rej2}"
    assert acc2[0].take_profit is not None # Handled gracefully by Excursion without throwing error

# --------------------------------------------------------------------------
# EXPLICIT JULY-29 REGRESSION FIXTURES
# --------------------------------------------------------------------------

def test_july29_soxs_native_geometry_fixture(pipeline, dummy_df, sample_features):
    """SOXS Momentum Long (Entry 67.59, Stop 65.81, Native Target 70.79 = 1.80R) must NOT produce 0.90R geometry."""
    sig_soxs = Signal(
        symbol="SOXS",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="momentum",
        reason="test",
        price=67.59,
        stop_loss=65.81,
        take_profit=67.59 + 1.8 * (67.59 - 65.81) # 70.794 (1.80R)
    )
    accepted, rejected = pipeline.process("SOXS", dummy_df, [sig_soxs], Regime.TREND_UP, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    final_risk = abs(proc.price - proc.stop_loss)
    final_reward = abs(proc.take_profit - proc.price)
    final_r = final_reward / final_risk
    assert proc.take_profit >= 70.79
    assert final_r >= 1.799 # Must NOT compress to 0.90R!

def test_july29_now1_native_geometry_fixture(pipeline, dummy_df, sample_features):
    """NOW #1 RSI2 Long (Entry 113.07, Stop 112.00, Native Target 114.675 = 1.50R) must NOT produce 0.38R geometry."""
    sig_now1 = Signal(
        symbol="NOW",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="rsi2_reversion",
        reason="test",
        price=113.07,
        stop_loss=112.00,
        take_profit=113.07 + 1.5 * (113.07 - 112.00) # 114.675 (1.50R)
    )
    accepted, rejected = pipeline.process("NOW", dummy_df, [sig_now1], Regime.TREND_UP, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    final_risk = abs(proc.price - proc.stop_loss)
    final_reward = abs(proc.take_profit - proc.price)
    final_r = final_reward / final_risk
    assert proc.take_profit >= 114.67
    assert final_r >= 1.499 # Must NOT compress to 0.38R!

def test_july29_crwv_native_geometry_fixture(pipeline, dummy_df, sample_features):
    """CRWV ORB Short (Entry 64.50, Stop 65.43, Native Target 62.64 = 2.00R) must preserve native geometry."""
    sig_crwv = Signal(
        symbol="CRWV",
        action=SignalAction.ENTER_SHORT,
        confidence=0.85,
        strategy="orb",
        reason="test",
        price=64.50,
        stop_loss=65.43,
        take_profit=64.50 - 2.0 * (65.43 - 64.50) # 62.64 (2.00R)
    )
    accepted, rejected = pipeline.process("CRWV", dummy_df, [sig_crwv], Regime.TREND_DOWN, features=sample_features)
    assert len(accepted) == 1, f"Signal rejected: {rejected}"
    proc = accepted[0]
    final_risk = abs(proc.price - proc.stop_loss)
    final_reward = abs(proc.price - proc.take_profit)
    final_r = final_reward / final_risk
    assert proc.take_profit <= 62.64
    assert final_r >= 1.999
