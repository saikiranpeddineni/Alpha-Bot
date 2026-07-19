"""Phase 2 quantitative unit tests.

Tests meta-labeling, MAE/MFE excursion regressors, regime-specific adaptive
models, and full pipeline integration with the new Phase 2 components.

Run with:
    .venv\\Scripts\\python.exe -m pytest backend/tests/test_quantitative_p2.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

class DummyClf:
    def predict_proba(self, x):
        import numpy as np
        return np.array([[0.1, 0.9]]) # 0.9 probability of class 1

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_quantitative.py helpers)
# ---------------------------------------------------------------------------

def _make_bar_df(n_bars: int = 200, seed: int = 42) -> pd.DataFrame:
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    base = datetime(2025, 1, 2, 9, 31, tzinfo=et).astimezone(UTC)
    index = pd.date_range(base, periods=n_bars, freq="1min", tz="UTC")
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 0.05, n_bars))
    close = np.clip(close, 1.0, None)
    high = close + rng.uniform(0, 0.3, n_bars)
    low = close - rng.uniform(0, 0.3, n_bars)
    open_ = close + rng.uniform(-0.2, 0.2, n_bars)
    volume = rng.integers(50_000, 500_000, n_bars).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _make_multi_day_df(n_days: int = 20, seed: int = 0) -> pd.DataFrame:
    frames = []
    for d in range(n_days):
        df = _make_bar_df(n_bars=390, seed=seed + d)
        # shift the index to make unique days
        shift = timedelta(days=d)
        df.index = df.index + shift
        frames.append(df)
    return pd.concat(frames).sort_index()


def _enriched_multi_day(n_days: int = 20) -> pd.DataFrame:
    from app.indicators.ta import enrich
    return enrich(_make_multi_day_df(n_days))


# ---------------------------------------------------------------------------
# 1. MAE/MFE dataset builder
# ---------------------------------------------------------------------------

class TestMaeMfeDataset:
    def test_returns_three_series(self):
        from app.ai.features import build_mae_mfe_dataset
        df = _enriched_multi_day(5)
        X, mae, mfe = build_mae_mfe_dataset(df, horizon_bars=10)
        assert len(X) == len(mae) == len(mfe)
        assert len(X) > 0

    def test_mae_non_negative(self):
        from app.ai.features import build_mae_mfe_dataset
        df = _enriched_multi_day(5)
        _, mae, _ = build_mae_mfe_dataset(df, horizon_bars=10)
        assert (mae >= 0).all(), "MAE contains negative values"

    def test_mfe_non_negative(self):
        from app.ai.features import build_mae_mfe_dataset
        df = _enriched_multi_day(5)
        _, _, mfe = build_mae_mfe_dataset(df, horizon_bars=10)
        assert (mfe >= 0).all(), "MFE contains negative values"

    def test_empty_df_returns_empty(self):
        from app.ai.features import build_mae_mfe_dataset
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        X, mae, mfe = build_mae_mfe_dataset(df, horizon_bars=10)
        assert len(X) == 0

    def test_feature_columns_present(self):
        from app.ai.features import build_mae_mfe_dataset, FEATURE_COLUMNS
        df = _enriched_multi_day(5)
        X, _, _ = build_mae_mfe_dataset(df, horizon_bars=10)
        assert list(X.columns) == FEATURE_COLUMNS


# ---------------------------------------------------------------------------
# 2. Excursion Regressors
# ---------------------------------------------------------------------------

class TestExcursionRegressors:
    def setup_method(self, tmp_path_factory=None):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()

    def _make_regressors(self):
        from app.ai.prediction import ExcursionRegressors
        return ExcursionRegressors(self._tmpdir)

    def test_atr_fallback_when_untrained(self):
        import pandas as pd
        from app.ai.features import FEATURE_COLUMNS
        reg = self._make_regressors()
        features = pd.Series(np.zeros(len(FEATURE_COLUMNS)), index=FEATURE_COLUMNS)
        est = reg.predict(features, atr_pct=1.0)
        # ATR fallback: mae=1.5%, mfe=2.0%
        assert est.mae_pct == pytest.approx(1.5, rel=0.01)
        assert est.mfe_pct == pytest.approx(2.0, rel=0.01)

    def test_reward_risk_positive(self):
        import pandas as pd
        from app.ai.features import FEATURE_COLUMNS
        reg = self._make_regressors()
        features = pd.Series(np.zeros(len(FEATURE_COLUMNS)), index=FEATURE_COLUMNS)
        est = reg.predict(features, atr_pct=1.0)
        assert est.reward_risk > 0

    def test_train_and_predict(self):
        reg = self._make_regressors()
        df = _enriched_multi_day(10)
        result = reg.train({"SYM": df}, horizon_bars=10)
        assert result["status"] == "success"
        assert reg.is_trained

        from app.ai.features import FEATURE_COLUMNS
        features = pd.Series(np.zeros(len(FEATURE_COLUMNS)), index=FEATURE_COLUMNS)
        est = reg.predict(features, atr_pct=1.0)
        assert est.mae_pct >= 0
        assert est.mfe_pct >= 0

    def test_from_atr_classmethod(self):
        from app.ai.prediction import ExcursionEstimate
        est = ExcursionEstimate.from_atr(2.0)
        assert est.mae_pct == pytest.approx(3.0, rel=0.01)
        assert est.mfe_pct == pytest.approx(4.0, rel=0.01)
        assert est.reward_risk > 0
        assert est.suggested_stop_pct > 0
        assert est.suggested_target_pct > 0


# ---------------------------------------------------------------------------
# 3. Meta-Label Engine
# ---------------------------------------------------------------------------

class TestMetaLabelEngine:
    def setup_method(self, tmp_path_factory=None):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()

    def _make_engine(self):
        from app.ai.prediction import MetaLabelEngine
        return MetaLabelEngine(self._tmpdir)

    def test_untrained_returns_neutral_approved(self):
        import pandas as pd
        from app.ai.features import FEATURE_COLUMNS
        engine = self._make_engine()
        features = pd.Series(np.zeros(len(FEATURE_COLUMNS)), index=FEATURE_COLUMNS)
        result = engine.predict("orb", features, side="long")
        assert result.prob_profitable is None
        assert result.decision == "BYPASS"
        assert result.approved is True  # don't block when untrained

    def test_known_strategy_has_threshold(self):
        engine = self._make_engine()
        assert engine.DEFAULT_THRESHOLDS["orb"] == pytest.approx(0.55)
        assert engine.DEFAULT_THRESHOLDS["vwap_reversion"] == pytest.approx(0.52)

    def test_train_and_predict_known_class(self, monkeypatch):
        engine = self._make_engine()
        df = _enriched_multi_day(10)
        # Monkeypatch get_strategy_candidates to return all timestamps of df except the first 60
        monkeypatch.setattr(
            "app.ai.prediction.get_strategy_candidates",
            lambda strategy, df, symbol, side, strategy_overrides: list(df.index[60:])
        )
        # Use min_validation_auc=0.0 to avoid rejected status on low random OOS AUC
        result = engine.train("orb", {"SYM": df}, horizon_bars=10, threshold_pct=0.05, side="long", min_validation_auc=0.0)
        assert result["status"] == "success"
        assert result["n_samples"] > 0
        assert 0.0 <= result["base_rate"] <= 1.0

        from app.ai.features import FEATURE_COLUMNS
        features = pd.Series(np.random.randn(len(FEATURE_COLUMNS)), index=FEATURE_COLUMNS)
        pred = engine.predict("orb", features, side="long")
        assert 0.0 <= pred.prob_profitable <= 1.0
        assert isinstance(pred.approved, bool)

    def test_unknown_strategy_uses_fallback_threshold(self):
        import pandas as pd
        from app.ai.features import FEATURE_COLUMNS
        engine = self._make_engine()
        features = pd.Series(np.zeros(len(FEATURE_COLUMNS)), index=FEATURE_COLUMNS)
        pred = engine.predict("nonexistent_strategy", features, side="long")
        assert pred.threshold == engine.FALLBACK_THRESHOLD

    def test_skips_with_no_data(self):
        engine = self._make_engine()
        result = engine.train("orb", {}, side="long")
        assert result["status"] == "skipped"


# ---------------------------------------------------------------------------
# 4. Regime-specific adaptive models in SignalModel
# ---------------------------------------------------------------------------

class TestRegimeAdaptiveModels:
    def test_train_adaptive_with_regime_key(self, tmp_path):
        from app.ai.model import SignalModel
        from app.indicators.ta import enrich
        model = SignalModel(tmp_path / "models")
        df = enrich(_make_multi_day_df(3))
        frames = {"SYM": df}
        result = model.train_adaptive(frames, horizon_bars=5, threshold_pct=0.05,
                                       save_to_disk=False, regime_key="trend_up")
        assert result["status"] == "success"
        assert result["regime_key"] == "trend_up"
        assert "trend_up" in model._regime_adaptive_long

    def test_train_adaptive_global_slot_when_no_regime_key(self, tmp_path):
        from app.ai.model import SignalModel
        from app.indicators.ta import enrich
        model = SignalModel(tmp_path / "models")
        df = enrich(_make_multi_day_df(3))
        frames = {"SYM": df}
        result = model.train_adaptive(frames, horizon_bars=5, threshold_pct=0.05,
                                       save_to_disk=False)
        assert result["status"] == "success"
        assert result["regime_key"] is None
        assert model._adaptive_long is not None

    def test_regime_model_isolated_from_global(self, tmp_path):
        """Regime-specific models should not overwrite the global adaptive slot."""
        from app.ai.model import SignalModel
        from app.indicators.ta import enrich
        model = SignalModel(tmp_path / "models")
        df = enrich(_make_multi_day_df(3))
        frames = {"SYM": df}
        model.train_adaptive(frames, save_to_disk=False, regime_key="range")
        assert model._adaptive_long is None  # global slot untouched
        assert "range" in model._regime_adaptive_long


# ---------------------------------------------------------------------------
# 5. Pipeline with Phase 2 components
# ---------------------------------------------------------------------------

class TestPipelinePhase2:
    def _make_pipeline(self, tmp_dir: str):
        from unittest.mock import MagicMock
        from app.strategies.pipeline import SignalPipeline
        model = MagicMock()
        model.score_action.return_value = 0.72
        return SignalPipeline(model, ml_threshold=0.55)

    def test_untrained_meta_does_not_block(self, tmp_path):
        from app.core.domain import Regime, Signal, SignalAction
        from app.indicators.ta import enrich
        pipe = self._make_pipeline(str(tmp_path))
        df = enrich(_make_bar_df(100))
        sig = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG,
                     confidence=0.9, strategy="orb", reason="test",
                     price=150.0, stop_loss=148.0, take_profit=153.0)
        # With untrained meta, it should default approve (prob=None, decision=BYPASS, approved=True)
        accepted, rejected = pipe.process("AAPL", df, [sig], Regime.TREND_UP)
        # Accepted or rejected by Trade Score — meta should not be the blocker
        for _, reason in rejected:
            assert "Meta-model rejected" not in reason

    def test_excursion_fills_missing_stop_target(self, tmp_path):
        from app.core.domain import Regime, Signal, SignalAction
        from app.indicators.ta import enrich
        pipe = self._make_pipeline(str(tmp_path))
        df = enrich(_make_bar_df(100))
        # Signal with no stop/target
        sig = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG,
                     confidence=0.9, strategy="orb", reason="test", price=150.0)
        assert sig.stop_loss is None
        assert sig.take_profit is None
        pipe.process("AAPL", df, [sig], Regime.TREND_UP)
        # After processing, stop and target should be populated from excursion estimate
        assert sig.stop_loss is not None, "stop_loss should be set from excursion estimate"
        assert sig.take_profit is not None, "take_profit should be set from excursion estimate"
        assert sig.stop_loss < 150.0, "stop should be below entry"
        assert sig.take_profit > 150.0, "target should be above entry"

    def test_exit_always_passes_with_phase2(self, tmp_path):
        from app.core.domain import Regime, Signal, SignalAction
        from app.indicators.ta import enrich
        pipe = self._make_pipeline(str(tmp_path))
        pipe.model.score_action.return_value = 0.10  # very low ML
        df = enrich(_make_bar_df(100))
        sig = Signal(symbol="AAPL", action=SignalAction.EXIT,
                     confidence=1.0, strategy="orb", reason="stop hit")
        accepted, rejected = pipe.process("AAPL", df, [sig], Regime.VOLATILE)
        assert len(accepted) == 1
        assert len(rejected) == 0


class TestStrategyCandidatesRegression:
    def test_feature_permutation_inference(self, monkeypatch):
        # Create temporary MetaLabelEngine
        import tempfile
        from app.ai.prediction import MetaLabelEngine
        tmpdir = tempfile.mkdtemp()
        engine = MetaLabelEngine(tmpdir)

        # Train a model
        df = _enriched_multi_day(10)
        # Monkeypatch get_strategy_candidates to return all timestamps of df except the first 60
        monkeypatch.setattr(
            "app.ai.prediction.get_strategy_candidates",
            lambda strategy, df, symbol, side, strategy_overrides: list(df.index[60:])
        )
        engine.train("orb", {"SYM": df}, horizon_bars=10, threshold_pct=0.05, side="long", min_validation_auc=0.0)

        from app.ai.features import FEATURE_COLUMNS
        # Create permuted raw series
        permuted_columns = list(FEATURE_COLUMNS)[::-1]
        vals = np.random.randn(len(FEATURE_COLUMNS))
        series_ordered = pd.Series(vals, index=FEATURE_COLUMNS)
        series_permuted = pd.Series(series_ordered.loc[permuted_columns].values, index=permuted_columns)

        pred_ordered = engine.predict("orb", series_ordered, side="long")
        pred_permuted = engine.predict("orb", series_permuted, side="long")

        # The probabilities must be identical because inputs are realigned to FEATURE_COLUMNS
        assert pred_ordered.prob_profitable == pred_permuted.prob_profitable

    def test_strategy_population_fingerprints_distinct(self):
        from app.ai.prediction import get_strategy_candidates
        df = _enriched_multi_day(15)
        
        # Generate candidates for 'orb' and 'momentum'
        orb_candidates = get_strategy_candidates("orb", df, "AAPL", "long")
        mom_candidates = get_strategy_candidates("momentum", df, "AAPL", "long")
        
        # Verify that candidate sets are not identical
        assert set(orb_candidates) != set(mom_candidates)


class TestSpecialistFallback:
    def test_specialist_fallback_logic(self, tmp_path, monkeypatch):
        from app.ai.model import SignalModel, ScoreActionResult
        from app.core.domain import SignalAction, Signal
        from app.strategies.pipeline import SignalPipeline
        from app.core.domain import Regime
        import joblib

        # 1. Setup SignalModel with a temp directory
        models_dir = tmp_path / "models"
        spec_dir = models_dir / "specialists"
        spec_dir.mkdir(parents=True)
        model = SignalModel(models_dir)

        # 2. Mock base model p_up and p_down to return fixed values
        monkeypatch.setattr(model, "p_up", lambda feats, regime_key=None: 0.65)
        monkeypatch.setattr(model, "p_down", lambda feats, regime_key=None: 0.35)

        # 3. Create dummy features Series
        from app.ai.features import FEATURE_COLUMNS
        import hashlib
        features = pd.Series(0.0, index=FEATURE_COLUMNS)
        current_hash = hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]

        # Test Case 1: Missing Specialist -> Falls back to base model (NO_ARTIFACT)
        res_missing = model.score_action_detailed(features, SignalAction.ENTER_LONG, "TSLA")
        assert res_missing.model_source == "BASE"
        assert res_missing.specialist_status == "NO_ARTIFACT"
        assert res_missing.probability == 0.65
        assert res_missing.fallback_reason is None

        # Test Case 2: Low-AUC Specialist (< 0.53) -> Falls back to base model (REJECTED_LOW_AUC)
        low_auc_spec = {
            "auc": 0.51,
            "trained_at": "2026-07-06",
            "n_samples": 100,
            "n_positive": 50,
            "n_negative": 50,
            "feature_version": current_hash,
            "long": None,
            "short": None,
        }
        spec_file = spec_dir / "tsla.pkl"
        joblib.dump(low_auc_spec, spec_file)
        
        # Reload specialists
        model._specialists.clear()
        if spec_dir.exists():
            for pkl in spec_dir.glob("*.pkl"):
                model._specialists[pkl.stem.upper()] = joblib.load(pkl)

        res_low_auc = model.score_action_detailed(features, SignalAction.ENTER_LONG, "TSLA")
        assert res_low_auc.model_source == "BASE"
        assert res_low_auc.specialist_status == "REJECTED_LOW_AUC"
        assert res_low_auc.probability == 0.65
        assert "below minimum threshold" in res_low_auc.fallback_reason

        # Test Case 3: Valid Specialist (AUC >= 0.53) -> Uses specialist prediction
        valid_spec = {
            "auc": 0.60,
            "trained_at": "2026-07-06",
            "n_samples": 100,
            "n_positive": 50,
            "n_negative": 50,
            "feature_version": current_hash,
            "long": DummyClf(),
            "short": DummyClf(),
        }
        joblib.dump(valid_spec, spec_file)
        
        # Reload specialists
        model._specialists.clear()
        if spec_dir.exists():
            for pkl in spec_dir.glob("*.pkl"):
                model._specialists[pkl.stem.upper()] = joblib.load(pkl)

        res_valid = model.score_action_detailed(features, SignalAction.ENTER_LONG, "TSLA")
        assert res_valid.model_source == "SPECIALIST"
        assert res_valid.specialist_status == "SPECIALIST_USED"
        assert res_valid.probability == 0.90
        assert res_valid.fallback_reason is None

        # Test Case 4: score_action backwards compatibility
        assert model.score_action(features, SignalAction.ENTER_LONG, "TSLA") == 0.90

        # Test Case 5: Side-specific AUC check
        # auc_long is high, but auc_short is low. Short action should fail validation gate.
        side_auc_spec = {
            "auc": 0.58,
            "auc_long": 0.58,
            "auc_short": 0.51,  # below 0.53 threshold
            "trained_at": "2026-07-06",
            "n_samples": 100,
            "n_positive": 50,
            "n_negative": 50,
            "feature_version": current_hash,
            "long": DummyClf(),
            "short": DummyClf(),
        }
        joblib.dump(side_auc_spec, spec_file)
        model._specialists.clear()
        if spec_dir.exists():
            for pkl in spec_dir.glob("*.pkl"):
                model._specialists[pkl.stem.upper()] = joblib.load(pkl)

        # Long should PASS
        res_long = model.score_action_detailed(features, SignalAction.ENTER_LONG, "TSLA")
        assert res_long.model_source == "SPECIALIST"
        assert res_long.specialist_status == "SPECIALIST_USED"

        # Short should FALLBACK due to low side-specific AUC
        res_short = model.score_action_detailed(features, SignalAction.ENTER_SHORT, "TSLA")
        assert res_short.model_source == "BASE"
        assert res_short.specialist_status == "REJECTED_LOW_AUC"

        # Test Case 6: Feature version mismatch check
        mismatch_spec = {
            "auc": 0.60,
            "trained_at": "2026-07-06",
            "feature_version": "outdated_hash",
            "long": DummyClf(),
            "short": DummyClf(),
        }
        joblib.dump(mismatch_spec, spec_file)
        model._specialists.clear()
        if spec_dir.exists():
            for pkl in spec_dir.glob("*.pkl"):
                model._specialists[pkl.stem.upper()] = joblib.load(pkl)

        res_mismatch = model.score_action_detailed(features, SignalAction.ENTER_LONG, "TSLA")
        assert res_mismatch.model_source == "BASE"  # fell back

        # Test Case 7: Specialist temporal leakage bypass
        from datetime import datetime, UTC
        future_spec = {
            "auc": 0.60,
            "trained_at": "2026-07-08T00:00:00Z",  # after backtest start
            "feature_version": current_hash,
            "long": DummyClf(),
            "short": DummyClf(),
        }
        joblib.dump(future_spec, spec_file)
        model._specialists.clear()
        if spec_dir.exists():
            for pkl in spec_dir.glob("*.pkl"):
                model._specialists[pkl.stem.upper()] = joblib.load(pkl)

        model.backtest_start_time = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
        res_leak = model.score_action_detailed(features, SignalAction.ENTER_LONG, "TSLA")
        assert res_leak.model_source == "BASE"  # bypassed due to temporal leakage

        # Cleanup Specialist test files
        if spec_file.exists():
            spec_file.unlink()


def test_proportional_target_scaling():
    """Verify that when the stop loss percentage floor is hit, the take profit target is scaled proportionally."""
    from app.core.domain import Signal, SignalAction
    from app.strategies.pipeline import SignalPipeline
    from unittest.mock import MagicMock
    import pandas as pd

    # Mock settings to have min_stop_pct = 0.5% and w = [1.0]
    mock_settings = MagicMock()
    mock_settings.risk_config().min_stop_pct = 0.5
    mock_settings.trade_score_weights.return_value = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    mock_settings.risk_config().score_threshold_trend = 50.0

    # Mock model
    mock_model = MagicMock()
    score_action_res = MagicMock()
    score_action_res.probability = 0.60
    score_action_res.model_source = "BASE"
    score_action_res.specialist_status = "NO_ARTIFACT"
    score_action_res.specialist_auc = None
    score_action_res.specialist_min_auc = 0.53
    score_action_res.fallback_reason = None
    mock_model.score_action_detailed.return_value = score_action_res
    mock_model.score_action.return_value = 0.60

    pipeline = SignalPipeline(mock_model, ml_threshold=0.55)
    # Patch get_settings to return our mock settings
    import app.strategies.pipeline
    orig_get_settings = app.strategies.pipeline.get_settings
    app.strategies.pipeline.get_settings = MagicMock(return_value=mock_settings)

    try:
        # Create a signal with extremely narrow stop loss (e.g. 0.1% risk)
        # Entry = 100, Stop = 99.9, Target = 100.2 (RR = 2.0)
        sig = Signal(
            symbol="AAPL",
            action=SignalAction.ENTER_LONG,
            confidence=0.8,
            strategy="momentum",
            reason="test",
            price=100.0,
            stop_loss=99.9,
            take_profit=100.2
        )

        from app.core.domain import Regime
        df = pd.DataFrame({"close": [100.0]})
        features = pd.Series({"atr14": 1.0, "vol_z": 1.0, "rsi14": 50.0, "vwap_dist_pct": 0.0})

        accepted, rejected = pipeline.process("AAPL", df, [sig], Regime.TREND_UP, features)

        # Stop loss should be widened to 99.5 (0.5% min_stop_pct)
        assert sig.stop_loss == 99.5
        # Target should be scaled proportionally: orig risk = 0.1, new risk = 0.5, orig target dist = 0.2
        # scaled target dist = 0.5 * 2.0 = 1.0 -> target = 101.0
        assert sig.take_profit == 101.0
        # Verification that the RR ratio is preserved at 2.0
        assert abs(sig.take_profit - 100.0) / abs(100.0 - sig.stop_loss) == 2.0
    finally:
        app.strategies.pipeline.get_settings = orig_get_settings


def test_vwap_semantic_target_preserved_when_stop_floor_applied():
    """Verify that when min_stop_pct widens the stop for vwap_reversion, take_profit stays at the semantic VWAP target."""
    from app.core.domain import Signal, SignalAction
    from app.strategies.pipeline import SignalPipeline
    from unittest.mock import MagicMock
    import pandas as pd

    # Mock settings to have min_stop_pct = 0.5%
    mock_settings = MagicMock()
    mock_settings.risk_config().min_stop_pct = 0.5
    mock_settings.trade_score_weights.return_value = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    mock_settings.risk_config().score_threshold_trend = 50.0

    # Mock model
    mock_model = MagicMock()
    score_action_res = MagicMock()
    score_action_res.probability = 0.60
    score_action_res.model_source = "BASE"
    score_action_res.specialist_status = "NO_ARTIFACT"
    score_action_res.specialist_auc = None
    score_action_res.specialist_min_auc = 0.53
    score_action_res.fallback_reason = None
    mock_model.score_action_detailed.return_value = score_action_res
    mock_model.score_action.return_value = 0.60

    pipeline = SignalPipeline(mock_model, ml_threshold=0.55)
    # Patch get_settings to return our mock settings
    import app.strategies.pipeline
    orig_get_settings = app.strategies.pipeline.get_settings
    app.strategies.pipeline.get_settings = MagicMock(return_value=mock_settings)

    try:
        # Create a signal with strategy = "vwap_reversion"
        # Entry = 100, Stop = 99.9, Target = 100.5 (representing vwap)
        sig = Signal(
            symbol="AAPL",
            action=SignalAction.ENTER_LONG,
            confidence=0.8,
            strategy="vwap_reversion",
            reason="test",
            price=100.0,
            stop_loss=99.9,
            take_profit=100.5
        )

        from app.core.domain import Regime
        df = pd.DataFrame({"close": [100.0]})
        features = pd.Series({"atr14": 1.0, "vol_z": 1.0, "rsi14": 50.0, "vwap_dist_pct": 0.0})

        accepted, rejected = pipeline.process("AAPL", df, [sig], Regime.TREND_UP, features)

        # Stop loss should be widened to 99.5 (0.5% min_stop_pct)
        assert sig.stop_loss == 99.5
        # Target should NOT be scaled, preserving original vwap price 100.5
        assert sig.take_profit == 100.5
    finally:
        app.strategies.pipeline.get_settings = orig_get_settings


