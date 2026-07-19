"""055 — Signal confidence score range validation.

Verifies that _validate_signal_probability rejects out-of-range, NaN, infinite,
and boolean values, and that ScoreActionResult / score_action_detailed never
presents an unvalidated probability to the pipeline threshold comparison.
"""
from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.ai.model_legacy import (
    ScoreActionResult,
    SignalModel,
    _validate_signal_probability,
)
from app.core.domain import SignalAction


# ---------------------------------------------------------------------------
# _validate_signal_probability unit tests
# ---------------------------------------------------------------------------


class TestValidateSignalProbability:
    def test_valid_zero(self):
        p, reason = _validate_signal_probability(0.0)
        assert p == 0.0
        assert reason is None

    def test_valid_half(self):
        p, reason = _validate_signal_probability(0.5)
        assert p == 0.5
        assert reason is None

    def test_valid_one(self):
        p, reason = _validate_signal_probability(1.0)
        assert p == 1.0
        assert reason is None

    def test_invalid_negative(self):
        p, reason = _validate_signal_probability(-0.001)
        assert p is None
        assert reason is not None

    def test_invalid_above_one(self):
        p, reason = _validate_signal_probability(1.001)
        assert p is None
        assert reason is not None

    def test_invalid_large_positive(self):
        p, reason = _validate_signal_probability(1.5)
        assert p is None
        assert reason is not None

    def test_invalid_nan(self):
        p, reason = _validate_signal_probability(float("nan"))
        assert p is None
        assert reason is not None
        assert "NaN" in reason

    def test_invalid_positive_infinity(self):
        p, reason = _validate_signal_probability(float("inf"))
        assert p is None
        assert reason is not None
        assert "infinity" in reason

    def test_invalid_negative_infinity(self):
        p, reason = _validate_signal_probability(float("-inf"))
        assert p is None
        assert reason is not None
        assert "infinity" in reason

    def test_invalid_bool_true(self):
        """True is numerically 1 — must still be rejected."""
        p, reason = _validate_signal_probability(True)
        assert p is None
        assert reason is not None
        assert "bool" in reason

    def test_invalid_bool_false(self):
        """False is numerically 0 — must still be rejected."""
        p, reason = _validate_signal_probability(False)
        assert p is None
        assert reason is not None
        assert "bool" in reason

    def test_invalid_string(self):
        p, reason = _validate_signal_probability("0.5")
        assert p is None
        assert reason is not None

    def test_invalid_none_type(self):
        p, reason = _validate_signal_probability(None)
        assert p is None
        assert reason is not None


# ---------------------------------------------------------------------------
# Integration: invalid score does not authorise an entry via pipeline
# ---------------------------------------------------------------------------


class TestPipelineInvalidScoreFailClosed:
    """Pipeline must treat INVALID_SCORE as fail-closed (same as ml=None)."""

    def _make_pipeline(self, model):
        from app.strategies.pipeline import SignalPipeline
        with patch("app.ai.prediction.MetaLabelEngine"):
            pl = SignalPipeline(model=model, ml_threshold=0.55)
        return pl

    def _make_signal(self, action=SignalAction.ENTER_LONG):
        from app.core.domain import Signal
        sig = MagicMock(spec=Signal)
        sig.action = action
        sig.strategy = "momentum"
        sig.ml_probability = None
        sig.regime = None
        sig.price = 100.0
        sig.stop_loss = None
        sig.take_profit = None
        sig.score = None
        return sig

    def _make_features(self):
        from app.ai.features import FEATURE_COLUMNS
        return pd.Series({col: 0.0 for col in FEATURE_COLUMNS})

    def _make_df(self):
        return pd.DataFrame(
            {"close": [100.0], "atr14": [1.0], "vol_sma20": [1_000_000.0]}
        )

    @pytest.mark.parametrize("bad_prob", [
        1.5,
        -0.2,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
    ])
    def test_invalid_prob_does_not_authorise_entry(self, bad_prob):
        """An out-of-range probability must never authorise an entry."""
        model = MagicMock()
        model.score_action_detailed.return_value = ScoreActionResult(
            probability=bad_prob,
            model_source="BASE",
            specialist_status="BASE",
            specialist_auc=None,
            specialist_min_auc=0.53,
            fallback_reason=None,
        )
        # score_action_detailed is detected as real (not Mock)
        type(model.score_action_detailed).__name__ = "method"

        features = self._make_features()
        sig = self._make_signal()
        df = self._make_df()

        from app.core.domain import Regime
        pl = self._make_pipeline(model)
        accepted, rejected = pl.process(
            symbol="TEST",
            df=df,
            signals=[sig],
            regime=Regime.TREND_UP,
            features=features,
        )
        assert accepted == [], f"Entry must not be authorised for probability={bad_prob!r}"

    def test_valid_prob_passes_through(self):
        """A valid probability above threshold must still be accepted."""
        model = MagicMock()
        model.score_action_detailed.return_value = ScoreActionResult(
            probability=0.80,
            model_source="BASE",
            specialist_status="BASE",
            specialist_auc=None,
            specialist_min_auc=0.53,
            fallback_reason=None,
        )
        type(model.score_action_detailed).__name__ = "method"

        features = self._make_features()
        sig = self._make_signal()
        df = self._make_df()

        from app.core.domain import Regime
        pl = self._make_pipeline(model)
        # trade score gate may still reject; we only check the ML gate does not block
        accepted, rejected = pl.process(
            symbol="TEST",
            df=df,
            signals=[sig],
            regime=Regime.TREND_UP,
            features=features,
        )
        # ML gate passed — signal advanced past Stage 1
        ml_rejected = [
            r for _, r in rejected
            if "ML confidence" in r and "< threshold" in r
        ]
        assert ml_rejected == [], "Valid prob above threshold must pass Stage 1"

    def test_valid_prob_below_threshold_is_rejected(self):
        """A valid in-range probability below threshold is rejected with normal message."""
        model = MagicMock()
        model.score_action_detailed.return_value = ScoreActionResult(
            probability=0.40,
            model_source="BASE",
            specialist_status="BASE",
            specialist_auc=None,
            specialist_min_auc=0.53,
            fallback_reason=None,
        )
        type(model.score_action_detailed).__name__ = "method"

        features = self._make_features()
        sig = self._make_signal()
        df = self._make_df()

        from app.core.domain import Regime
        pl = self._make_pipeline(model)
        accepted, rejected = pl.process(
            symbol="TEST",
            df=df,
            signals=[sig],
            regime=Regime.TREND_UP,
            features=features,
        )
        reasons = [r for _, r in rejected]
        assert any("ML confidence" in r for r in reasons), "Below-threshold prob must be rejected with ML confidence message"


# ---------------------------------------------------------------------------
# score_action_detailed validates probabilities at the model boundary
# ---------------------------------------------------------------------------


class TestScoreActionDetailedValidation:
    """_make_validated_result must reject invalid raw probabilities."""

    def _make_model(self, tmp_path: Path) -> SignalModel:
        with patch("app.config.get_settings") as mock_settings:
            mock_rc = MagicMock()
            mock_rc.min_specialist_auc = 0.53
            mock_settings.return_value.risk_config.return_value = mock_rc
            model = SignalModel.__new__(SignalModel)
            model.models_dir = tmp_path
            model._model_long = None
            model._model_short = None
            model._adaptive_long = None
            model._adaptive_short = None
            model._regime_adaptive_long = {}
            model._regime_adaptive_short = {}
            model._specialists = {}
            model._auc_warned_symbols = set()
            model.backtest_start_time = None
            model.meta = {}
            return model

    @pytest.mark.parametrize("bad_prob", [1.5, -0.001, float("nan"), float("inf"), float("-inf"), True, False])
    def test_invalid_base_prob_yields_none(self, tmp_path, bad_prob):
        """_make_validated_result must map invalid raw probability to probability=None."""
        model = self._make_model(tmp_path)
        result = model._make_validated_result(
            raw_prob=bad_prob,
            model_source="BASE",
            specialist_status="BASE",
            auc=None,
            min_auc=0.53,
            fallback_reason=None,
        )
        assert result.probability is None
        assert result.specialist_status == "INVALID_SCORE"
        assert result.fallback_reason is not None

    @pytest.mark.parametrize("good_prob", [0.0, 0.5, 1.0, 0.9999])
    def test_valid_base_prob_passes_through(self, tmp_path, good_prob):
        model = self._make_model(tmp_path)
        result = model._make_validated_result(
            raw_prob=good_prob,
            model_source="BASE",
            specialist_status="BASE",
            auc=None,
            min_auc=0.53,
            fallback_reason=None,
        )
        assert result.probability == good_prob
        assert result.specialist_status == "BASE"

    def test_paper_and_live_share_same_validation(self, tmp_path):
        """Validation is in _make_validated_result — called on both paths."""
        model = self._make_model(tmp_path)
        for bad in [float("nan"), 1.5]:
            r = model._make_validated_result(bad, "BASE", "BASE", None, 0.53, None)
            assert r.probability is None
