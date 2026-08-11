from app.config import Settings
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
    def __init__(self, prob=0.38, model_source="BASE", specialist_status="NO_ARTIFACT"):
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
        "rsi2": [5.0] * 50,
        "ema50": [98.0] * 50,
    }, index=idx)
    return df


def dummy_features() -> pd.Series:
    return pd.Series({col: 1.0 for col in FEATURE_COLUMNS})


def test_rsi2_base_fallback_veto_enforced_1130():
    """Test 1: RSI2 LONG at 11:30 ET with BASE_FALLBACK ML < 0.52 -> Strictly REJECTED by ml_gate."""
    mock_model = DummySignalModel(prob=0.38, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52, risk_config=RiskConfig(), settings=Settings())
    
    ts_et = datetime(2026, 7, 21, 11, 30, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.6,
        strategy="rsi2_reversion",
        reason="RSI(2) dip",
        price=100.0,
        stop_loss=98.5,
        take_profit=101.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.RANGE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 1


def test_rsi2_real_model_not_bypassed_1130():
    """Test 2: RSI2 LONG at 11:30 ET with REAL_MODEL / Specialist ML < 0.52 -> REJECTED by ml_gate."""
    mock_model = DummySignalModel(prob=0.38, model_source="SPECIALIST", specialist_status="SPECIALIST_USED")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52, risk_config=RiskConfig(), settings=Settings())
    
    ts_et = datetime(2026, 7, 21, 11, 30, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.6,
        strategy="rsi2_reversion",
        reason="RSI(2) dip",
        price=100.0,
        stop_loss=98.5,
        take_profit=101.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.RANGE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 1


def test_momentum_base_fallback_not_bypassed_1130():
    """Test 3: Momentum LONG at 11:30 ET with BASE_FALLBACK ML < 0.52 -> REJECTED by ml_gate."""
    mock_model = DummySignalModel(prob=0.38, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52, risk_config=RiskConfig(), settings=Settings())
    
    ts_et = datetime(2026, 7, 21, 11, 30, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.6,
        strategy="momentum",
        reason="EMA cross",
        price=100.0,
        stop_loss=98.5,
        take_profit=101.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.RANGE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 1


def test_rsi2_base_fallback_veto_enforced_1330():
    """Test 4: RSI2 LONG at 13:30 ET with BASE_FALLBACK ML < 0.52 -> Strictly REJECTED by ml_gate."""
    mock_model = DummySignalModel(prob=0.38, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52, risk_config=RiskConfig(), settings=Settings())
    
    ts_et = datetime(2026, 7, 21, 13, 30, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.6,
        strategy="rsi2_reversion",
        reason="RSI(2) dip",
        price=100.0,
        stop_loss=98.5,
        take_profit=101.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.RANGE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 1


def test_rsi2_base_fallback_outside_window_1430():
    """Test 5: RSI2 LONG at 14:30 ET with BASE_FALLBACK ML < 0.52 -> REJECTED by ml_gate outside 11:00-14:00 ET window."""
    mock_model = DummySignalModel(prob=0.38, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52, risk_config=RiskConfig(), settings=Settings())
    
    ts_et = datetime(2026, 7, 21, 14, 30, tzinfo=ET)
    df = make_dummy_df(ts_et)
    feats = dummy_features()
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.6,
        strategy="rsi2_reversion",
        reason="RSI(2) dip",
        price=100.0,
        stop_loss=98.5,
        take_profit=101.5
    )
    
    accepted, rejected = pipeline.process(
        symbol="AAPL",
        df=df,
        signals=[sig],
        regime=Regime.RANGE,
        features=feats,
    )
    
    assert getattr(sig, "ml_gate_decision", None) == "REJECT"
    ml_rejections = [r for s, r in rejected if "ML confidence" in r]
    assert len(ml_rejections) == 1


def test_strict_ml_veto_across_times():
    """Verify ML gate vetoes weak signals strictly across all time horizons."""
    mock_model = DummySignalModel(prob=0.38, model_source="BASE", specialist_status="NO_ARTIFACT")
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.52, risk_config=RiskConfig(), settings=Settings())
    feats = dummy_features()
    
    times = [
        datetime(2026, 7, 21, 9, 35, tzinfo=ET),
        datetime(2026, 7, 21, 10, 59, tzinfo=ET),
        datetime(2026, 7, 21, 11, 0, tzinfo=ET),
        datetime(2026, 7, 21, 13, 59, tzinfo=ET),
        datetime(2026, 7, 21, 14, 0, tzinfo=ET),
        datetime(2026, 7, 21, 15, 45, tzinfo=ET),
    ]
    
    for dt in times:
        df = make_dummy_df(dt)
        sig = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=0.6, strategy="rsi2_reversion", reason="dip", price=100.0, stop_loss=98.5, take_profit=101.5)
        accepted, rejected = pipeline.process("AAPL", df, [sig], Regime.RANGE, features=feats)
        assert getattr(sig, "ml_gate_decision", None) == "REJECT"
        assert len(rejected) >= 1
        assert any("ML confidence" in r for _, r in rejected)
