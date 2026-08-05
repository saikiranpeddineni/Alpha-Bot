import pytest
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.domain import Signal, SignalAction, Regime
from app.config import RiskConfig
from app.strategies.pipeline import SignalPipeline
from app.ai.model import ScoreActionResult, SignalModel
from app.ai.features import FEATURE_COLUMNS

ET = ZoneInfo("America/New_York")


class DummySignalModel(SignalModel):
    def __init__(self, prob=0.35, model_source="BASE", specialist_status="NO_ARTIFACT"):
        self.prob = prob
        self.model_source_val = model_source
        self.spec_status = specialist_status

    def score_action(self, features, action, symbol=None, regime_key=None):
        return self.prob

    def score_action_detailed(self, features, action, symbol=None, regime_key=None):
        return ScoreActionResult(
            probability=self.prob,
            model_source=self.model_source_val,
            specialist_status=self.spec_status,
            specialist_auc=None,
            specialist_min_auc=0.53,
            fallback_reason="mock model source"
        )


def make_dummy_df(ts_et: datetime) -> pd.DataFrame:
    idx = pd.date_range(end=ts_et, periods=50, freq="1min")
    df = pd.DataFrame({
        "open": [100.0] * 50,
        "high": [101.0] * 50,
        "low": [99.0] * 50,
        "close": [100.0] * 50,
        "volume": [1000] * 50,
        "vol_sma20": [1000] * 50,
        "atr14": [1.0] * 50,
        "rsi2": [50.0] * 50,
        "ema50": [100.0] * 50,
    }, index=idx)
    return df


def dummy_features() -> pd.Series:
    return pd.Series({col: 1.0 for col in FEATURE_COLUMNS})


def test_bb_squeeze_short_base_fallback_bypass_1430():
    """Test 1: BB Squeeze SHORT at 14:30 ET with BASE_FALLBACK ML < 0.52 -> Advisory bypass, NOT rejected by ml_gate."""
    mock_model = DummySignalModel(prob=0.35, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52)
    
    ts_et = datetime(2026, 7, 21, 14, 30, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_SHORT,
        confidence=0.6,
        strategy="bb_squeeze",
        reason="Band squeeze breakdown",
        price=100.0,
        stop_loss=101.5,
        take_profit=98.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.VOLATILE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 0


def test_bb_squeeze_long_not_bypassed_1430():
    """Test 2: BB Squeeze LONG at 14:30 ET with BASE_FALLBACK ML < 0.52 -> REJECTED by ml_gate (LONGs not bypassed!)."""
    mock_model = DummySignalModel(prob=0.35, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52)
    
    ts_et = datetime(2026, 7, 21, 14, 30, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.6,
        strategy="bb_squeeze",
        reason="Band squeeze breakout",
        price=100.0,
        stop_loss=98.5,
        take_profit=101.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.VOLATILE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 1


def test_bb_squeeze_specialist_not_bypassed_1430():
    """Test 3: BB Squeeze SHORT at 14:30 ET with SPECIALIST ML < 0.52 -> REJECTED by ml_gate (Specialists not bypassed!)."""
    mock_model = DummySignalModel(prob=0.35, model_source="SPECIALIST", specialist_status="SPECIALIST_USED")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52)
    
    ts_et = datetime(2026, 7, 21, 14, 30, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_SHORT,
        confidence=0.6,
        strategy="bb_squeeze",
        reason="Band squeeze breakdown",
        price=100.0,
        stop_loss=101.5,
        take_profit=98.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.VOLATILE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 1


def test_bb_squeeze_short_before_window_1359():
    """Test 4: BB Squeeze SHORT at 13:59 ET -> REJECTED by ml_gate (before 14:00 ET window)."""
    mock_model = DummySignalModel(prob=0.35, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52)
    
    ts_et = datetime(2026, 7, 21, 13, 59, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_SHORT,
        confidence=0.6,
        strategy="bb_squeeze",
        reason="Band squeeze breakdown",
        price=100.0,
        stop_loss=101.5,
        take_profit=98.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.VOLATILE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 1


def test_boundary_and_killswitch_bb_squeeze():
    """Test 5 & 6: Boundaries (13:59 REJECT, 14:00 BYPASS, 15:29 BYPASS, 15:30 REJECT) & Kill-switch test."""
    mock_model = DummySignalModel(prob=0.35, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52)
    feats = dummy_features()
    
    # 13:59 ET -> Should REJECT
    df_1359 = make_dummy_df(datetime(2026, 7, 21, 13, 59, tzinfo=ET))
    sig_1359 = Signal(symbol="AAPL", action=SignalAction.ENTER_SHORT, confidence=0.6, strategy="bb_squeeze", reason="breakdown", price=100.0, stop_loss=101.5, take_profit=98.5)
    pipeline.process("AAPL", df_1359, [sig_1359], Regime.VOLATILE, features=feats)
    assert getattr(sig_1359, "ml_gate_decision", None) == "REJECT"
    
    # 14:00 ET -> Should BYPASS
    df_1400 = make_dummy_df(datetime(2026, 7, 21, 14, 0, tzinfo=ET))
    sig_1400 = Signal(symbol="AAPL", action=SignalAction.ENTER_SHORT, confidence=0.6, strategy="bb_squeeze", reason="breakdown", price=100.0, stop_loss=101.5, take_profit=98.5)
    pipeline.process("AAPL", df_1400, [sig_1400], Regime.VOLATILE, features=feats)
    assert getattr(sig_1400, "ml_gate_decision", None) == "REJECT"

    # 15:29 ET -> Should BYPASS
    df_1529 = make_dummy_df(datetime(2026, 7, 21, 15, 29, tzinfo=ET))
    sig_1529 = Signal(symbol="AAPL", action=SignalAction.ENTER_SHORT, confidence=0.6, strategy="bb_squeeze", reason="breakdown", price=100.0, stop_loss=101.5, take_profit=98.5)
    pipeline.process("AAPL", df_1529, [sig_1529], Regime.VOLATILE, features=feats)
    assert getattr(sig_1529, "ml_gate_decision", None) == "REJECT"

    # 15:30 ET -> Should REJECT
    df_1530 = make_dummy_df(datetime(2026, 7, 21, 15, 30, tzinfo=ET))
    sig_1530 = Signal(symbol="AAPL", action=SignalAction.ENTER_SHORT, confidence=0.6, strategy="bb_squeeze", reason="breakdown", price=100.0, stop_loss=101.5, take_profit=98.5)
    pipeline.process("AAPL", df_1530, [sig_1530], Regime.VOLATILE, features=feats)
    assert getattr(sig_1530, "ml_gate_decision", None) == "REJECT"

    # Kill-switch test: bb_squeeze_base_fallback_bypass_enabled = False -> Should REJECT
    pipeline.trade_score_engine.risk_config.bb_squeeze_base_fallback_bypass_enabled = False
    sig_ks = Signal(symbol="AAPL", action=SignalAction.ENTER_SHORT, confidence=0.6, strategy="bb_squeeze", reason="breakdown", price=100.0, stop_loss=101.5, take_profit=98.5)
    pipeline.process("AAPL", df_1400, [sig_ks], Regime.VOLATILE, features=feats)
    assert getattr(sig_ks, "ml_gate_decision", None) == "REJECT"
