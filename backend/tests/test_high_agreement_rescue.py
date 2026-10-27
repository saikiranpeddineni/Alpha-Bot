from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from app.ai.features import FEATURE_COLUMNS
from app.ai.prediction import MetaPrediction
from app.config import RiskConfig
from app.core.domain import Regime, Signal, SignalAction
from app.strategies.pipeline import SignalPipeline


class _Model:
    def __init__(self, probability: float) -> None:
        self.probability = probability

    def score_action(self, features, action, symbol=None, regime_key=None):  # noqa: ANN001
        return self.probability


def _frame() -> pd.DataFrame:
    index = pd.date_range("2026-10-20 14:00", periods=40, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * 40,
            "high": [100.2] * 40,
            "low": [99.8] * 40,
            "close": [100.0] * 40,
            "volume": [100_000.0] * 40,
            "vol_sma20": [100_000.0] * 40,
            "atr14": [1.0] * 40,
        },
        index=index,
    )


def _pipeline(*, raw_score: float, meta_probability: float, model_probability: float = 0.70) -> SignalPipeline:
    risk_config = RiskConfig(
        enable_opening_blackout=False,
        enable_entry_chase_veto=False,
        enable_entry_location_veto=False,
        enable_entry_extreme_guard=False,
        enable_monte_carlo_gate=False,
    )
    pipeline = SignalPipeline(
        model=_Model(model_probability),
        ml_threshold=0.55,
        risk_config=risk_config,
        regime_weights={"momentum": {Regime.RANGE: 0.70}},
    )
    pipeline.meta_label_engine = SimpleNamespace(
        default_thresholds={"momentum": 0.55},
        FALLBACK_THRESHOLD=0.53,
        predict=lambda *args, **kwargs: MetaPrediction(
            strategy="momentum",
            prob_profitable=meta_probability,
            approved=True,
            threshold=0.55,
            n_training_samples=100,
            source="MOCK",
            decision="PASS",
            reason="",
        ),
    )
    pipeline.trade_score_engine.compute = MagicMock(return_value=raw_score)
    return pipeline


def _signal() -> Signal:
    return Signal(
        symbol="COIN",
        action=SignalAction.ENTER_LONG,
        confidence=0.80,
        strategy="momentum",
        reason="test rescue",
        price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
    )


def _process(pipeline: SignalPipeline, signal: Signal):
    features = pd.Series({column: 1.0 for column in FEATURE_COLUMNS})
    return pipeline.process("COIN", _frame(), [signal], Regime.RANGE, features=features)


def test_rescue_denied_when_raw_trade_score_floor_is_not_met():
    # =============================================================================
    # [MODIFIED 2026-10-25]: Align with 2026-10-24 Calibration of score_threshold_range to 65.0
    # ORIGINAL INTENT: raw_score=65.0 tested rejection against old 70.0 floor.
    # REASON FOR CHANGE: With threshold calibrated to 65.0, raw_score=60.0 tests floor denial.
    # SAFE ROLLBACK: Previous line preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # pipeline = _pipeline(raw_score=65.0, meta_probability=0.90)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    pipeline = _pipeline(raw_score=60.0, meta_probability=0.90)
    # =============================================================================
    signal = _signal()

    accepted, rejected = _process(pipeline, signal)

    assert accepted == []
    assert rejected and rejected[0][0] is signal
    assert signal.ml_gate_decision != "HIGH_AGREEMENT_RESCUE"


def test_rescue_requires_numeric_meta_floor_even_when_meta_says_pass():
    # =============================================================================
    # [MODIFIED 2026-10-22]: Update test meta_probability to test below calibrated 0.48 floor
    # ORIGINAL INTENT: meta_probability=0.6543 tested rejection against old 0.80 ceiling.
    # REASON FOR CHANGE: With rescue_meta_min calibrated to 0.48, test with 0.42 to verify floor rejection.
    # SAFE ROLLBACK: Previous line preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # pipeline = _pipeline(raw_score=80.5, meta_probability=0.6543)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    pipeline = _pipeline(raw_score=80.5, meta_probability=0.42)
    # =============================================================================
    signal = _signal()

    accepted, rejected = _process(pipeline, signal)

    assert accepted == []
    assert rejected and rejected[0][0] is signal
    assert signal.ml_gate_decision != "HIGH_AGREEMENT_RESCUE"


def test_rescue_works_with_consistent_scores_above_the_floor():
    pipeline = _pipeline(raw_score=80.5, meta_probability=0.84)
    signal = _signal()

    accepted, rejected = _process(pipeline, signal)

    assert len(accepted) == 1
    assert rejected == []
    assert signal.ml_gate_decision == "HIGH_AGREEMENT_RESCUE"
    # =============================================================================
    # [MODIFIED 2026-10-22]: Update expected rescue risk_multiplier from 0.75 to 0.65
    # ORIGINAL INTENT: rescue_risk_mult was 0.75.
    # REASON FOR CHANGE: Calibrated to 0.65 for prudent sizing on rescued regime-penalized trades.
    # SAFE ROLLBACK: Previous line preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # assert signal.risk_multiplier == 0.75
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    assert signal.risk_multiplier == 0.65
    # =============================================================================


def test_rescue_telemetry_reports_raw_and_admission_scores_consistently():
    pipeline = _pipeline(raw_score=80.5, meta_probability=0.84)
    signal = _signal()

    with patch("app.strategies.pipeline.applog.info") as info:
        _process(pipeline, signal)

    messages = [call.args[1] for call in info.call_args_list if len(call.args) >= 2]
    rescue_message = next(message for message in messages if "HIGH_AGREEMENT_RESCUE" in message)
    # =============================================================================
    # [MODIFIED 2026-10-25]: Align Telemetry String Assertions with 65.0 Calibration
    # ORIGINAL INTENT: Asserted hardcoded 70.0 floor strings in log messages.
    # REASON FOR CHANGE: Calibrated score_threshold_range is 65.0, so logs output 65.0.
    # SAFE ROLLBACK: Previous lines preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # assert "raw_score=80.5>=70.0" in rescue_message
    # assert "admission_score=56.3<70.0" in rescue_message
    # assert "raw_score=80.5<70.0" not in rescue_message
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    assert "raw_score=80.5>=65.0" in rescue_message
    assert "admission_score=56.3<65.0" in rescue_message
    assert "raw_score=80.5<65.0" not in rescue_message
    # =============================================================================
