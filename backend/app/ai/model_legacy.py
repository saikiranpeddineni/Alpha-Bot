"""ML signal model.

A gradient-boosted classifier estimates the probability that price rises by
more than a cost-adjusted threshold over the next N bars. The AI signal
pipeline uses this probability to gate strategy signals (longs need high
p_up, shorts need low p_up).

Until a model has been trained, a transparent momentum-flavored heuristic
provides the same interface -- clearly labeled so the UI can say so.
Training runs locally on any data the platform can fetch; models persist to
data/models/ via joblib.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from dataclasses import dataclass
import numpy as np
import pandas as pd

from app.ai.features import FEATURE_COLUMNS, build_training_set
from app.ai.prediction import WalkForwardValidator
from app.core.domain import SignalAction
from app.core.logging import applog

MODEL_FILE = "signal_model.joblib"
META_FILE = "signal_model.meta.json"

@dataclass(frozen=True)
class ScoreActionResult:
    probability: float | None
    model_source: str              # "SPECIALIST" or "BASE"
    specialist_status: str         # "SPECIALIST_USED", "REJECTED_LOW_AUC", "NO_ARTIFACT", "NO_SYMBOL", "SPECIALIST_INFERENCE_FAILED"
    specialist_auc: float | None
    specialist_min_auc: float
    fallback_reason: str | None

def predict_proba_safe(clf, x: pd.DataFrame):
    """Predict proba safely matching the feature names contract of the fitted classifier."""
    if hasattr(clf, "feature_names_in_"):
        cols = list(clf.feature_names_in_)
        if list(x.columns) != cols and len(x.columns) == len(cols):
            x = pd.DataFrame(x.to_numpy(), columns=cols)
        return clf.predict_proba(x)
    else:
        return clf.predict_proba(x.to_numpy())


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


class SignalModel:

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._model_long = None
        self._model_short = None
        self._adaptive_long = None
        self._adaptive_short = None
        # Phase 2: regime-specific adaptive models
        # Keys: "trend_up", "trend_down", "range", "volatile"
        self._regime_adaptive_long: dict[str, object] = {}
        self._regime_adaptive_short: dict[str, object] = {}
        self._specialists: dict[str, dict] = {}
        # Set only while a historical replay is running.  Live engine scoring
        # must see an explicit None rather than reading a missing attribute.
        self.backtest_start_time: datetime | None = None
        self.meta: dict = {}
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        model_long_path = self.models_dir / "signal_model_long.joblib"
        model_short_path = self.models_dir / "signal_model_short.joblib"
        model_legacy_path = self.models_dir / "signal_model.joblib"
        meta_path = self.models_dir / META_FILE
        if meta_path.exists():
            try:
                import joblib

                self.meta = json.loads(meta_path.read_text())
                if model_long_path.exists() and model_short_path.exists():
                    self._model_long = joblib.load(model_long_path)
                    self._model_short = joblib.load(model_short_path)
                    applog.info("ai.model", f"Loaded trained dual models ({self.meta.get('trained_at', '?')})")
                elif model_legacy_path.exists():
                    self._model_long = joblib.load(model_legacy_path)
                    self._model_short = None
                    applog.info("ai.model", f"Loaded legacy single model ({self.meta.get('trained_at', '?')})")
            except Exception as exc:  # noqa: BLE001
                applog.error("ai.model", f"Failed to load global model: {exc}")
                self._model_long = None
                self._model_short = None
                self.meta = {}

        model_long_adaptive_path = self.models_dir / "signal_model_long_adaptive.joblib"
        model_short_adaptive_path = self.models_dir / "signal_model_short_adaptive.joblib"
        if model_long_adaptive_path.exists() and model_short_adaptive_path.exists():
            try:
                import joblib
                self._adaptive_long = joblib.load(model_long_adaptive_path)
                self._adaptive_short = joblib.load(model_short_adaptive_path)
                applog.info("ai.model", "Loaded daily adaptive dual models")
            except Exception as exc:
                applog.error("ai.model", f"Failed to load daily adaptive model: {exc}")
                self._adaptive_long = None
                self._adaptive_short = None
        else:
            self._adaptive_long = None
            self._adaptive_short = None

        # Load regime-specific adaptive models
        for regime_key in ("trend_up", "trend_down", "range", "volatile"):
            long_path = self.models_dir / f"adaptive_{regime_key}_long.joblib"
            short_path = self.models_dir / f"adaptive_{regime_key}_short.joblib"
            try:
                import joblib
                if long_path.exists():
                    self._regime_adaptive_long[regime_key] = joblib.load(long_path)
                if short_path.exists():
                    self._regime_adaptive_short[regime_key] = joblib.load(short_path)
            except Exception as exc:
                applog.debug("ai.model", f"Could not load regime model {regime_key}: {exc}")

        # Load specialist models
        specialists_dir = self.models_dir / "specialists"
        self._specialists.clear()
        if specialists_dir.exists():
            for pkl in specialists_dir.glob("*.pkl"):
                try:
                    import joblib
                    artifact = joblib.load(pkl)
                    # Backfill side-specific AUC from combined AUC for backward compatibility
                    if isinstance(artifact, dict):
                        if 'auc' in artifact:
                            if 'auc_long' not in artifact:
                                artifact['auc_long'] = artifact['auc']
                            if 'auc_short' not in artifact:
                                artifact['auc_short'] = artifact['auc']
                    self._specialists[pkl.stem.upper()] = artifact
                except Exception as exc:
                    applog.debug("ai.model", f"Could not load specialist {pkl.name}: {exc}")

    @property
    def is_trained(self) -> bool:
        return self._model_long is not None

    def info(self) -> dict:
        return {
            "trained": self.is_trained,
            "engine": self.meta.get("engine", "gradient_boosting" if self.is_trained else "heuristic"),
            "has_short_model": self._model_short is not None,
            "has_adaptive": self._adaptive_long is not None,
            **self.meta,
        }

    # ---- inference ----

    def p_up(self, features: pd.Series, regime_key: str | None = None) -> float:
        """Probability of a favorable upward move for the latest bar.

        If ``regime_key`` is provided and a regime-specific adaptive model
        has been trained, it will be used instead of the global adaptive model.
        """
        if self.is_trained:
            x = pd.DataFrame([features[FEATURE_COLUMNS].to_numpy(dtype=float)], columns=FEATURE_COLUMNS)
            p_global = float(predict_proba_safe(self._model_long, x)[0, 1])
            # Prefer regime-specific adaptive > global adaptive
            reg_model = self._regime_adaptive_long.get(regime_key or "")
            if reg_model is not None:
                p_adaptive = float(predict_proba_safe(reg_model, x)[0, 1])
                return 0.70 * p_global + 0.30 * p_adaptive
            if self._adaptive_long is not None:
                p_adaptive = float(predict_proba_safe(self._adaptive_long, x)[0, 1])
                return 0.70 * p_global + 0.30 * p_adaptive
            return p_global
        return self._heuristic_p_up(features)

    def p_down(self, features: pd.Series, regime_key: str | None = None) -> float:
        """Probability of a favorable downward move for the latest bar."""
        if self._model_short is not None:
            x = pd.DataFrame([features[FEATURE_COLUMNS].to_numpy(dtype=float)], columns=FEATURE_COLUMNS)
            p_global = float(predict_proba_safe(self._model_short, x)[0, 1])
            reg_model = self._regime_adaptive_short.get(regime_key or "")
            if reg_model is not None:
                p_adaptive = float(predict_proba_safe(reg_model, x)[0, 1])
                return 0.70 * p_global + 0.30 * p_adaptive
            if self._adaptive_short is not None:
                p_adaptive = float(predict_proba_safe(self._adaptive_short, x)[0, 1])
                return 0.70 * p_global + 0.30 * p_adaptive
            return p_global
        # Fallback to complement of p_up for legacy models
        return 1.0 - self.p_up(features)

    @staticmethod
    def _heuristic_p_up(f: pd.Series) -> float:
        """Momentum-flavored logistic score used before any model is trained."""
        z = (
            0.9 * math.tanh(float(f["ret_5"]) * 300.0)
            + 0.7 * math.tanh(float(f["ret_15"]) * 200.0)
            + 0.5 * math.tanh(float(f["macd_hist_norm"]) * 1.5)
            + 0.4 * max(-1.0, min(1.0, (float(f["rsi14"]) - 50.0) / 30.0))
            + 0.3 * max(-1.0, min(1.0, float(f["vwap_dist_pct"]) / 0.5))
        )
        return _sigmoid(z)

    def _get_base_result(self, features: pd.Series, action: SignalAction, specialist_status: str, auc: float | None, min_auc: float, fallback_reason: str | None) -> ScoreActionResult:
        bt_start = self.backtest_start_time
        base_trained_at_str = self.meta.get("trained_at")
        base_trained_at = None
        if base_trained_at_str:
            base_trained_at = datetime.fromisoformat(base_trained_at_str.replace("Z", "+00:00"))
            if base_trained_at.tzinfo is None:
                base_trained_at = base_trained_at.replace(tzinfo=UTC)
            else:
                base_trained_at = base_trained_at.astimezone(UTC)
        
        if base_trained_at and bt_start:
            if bt_start.tzinfo is None:
                bt_start = bt_start.replace(tzinfo=UTC)
            if base_trained_at > bt_start:
                applog.warning(
                    "ai.model", 
                    f"[BASE] Bypass BASE model due to temporal leakage | trained_at={base_trained_at_str} | backtest_start={bt_start.isoformat()} | model_source=BASE"
                )
                return ScoreActionResult(
                    probability=None,
                    model_source="BASE",
                    specialist_status="TEMPORAL_BYPASS",
                    specialist_auc=auc,
                    specialist_min_auc=min_auc,
                    fallback_reason="BASE model temporal leakage"
                )
                
        base_prob = self.p_down(features) if action == SignalAction.ENTER_SHORT else self.p_up(features)
        return ScoreActionResult(base_prob, "BASE", specialist_status, auc, min_auc, fallback_reason)

    def score_action_detailed(
        self, features: pd.Series | None, action: SignalAction, symbol: str | None = None
    ) -> ScoreActionResult:
        """Probability the move is favorable for the proposed action, with detailed routing metadata."""
        from app.config import get_settings
        min_auc = get_settings().risk_config().min_specialist_auc

        if features is None:
            return ScoreActionResult(0.5, "BASE", "NO_ARTIFACT", None, min_auc, "features is None")
        if not all(col in features.index for col in FEATURE_COLUMNS):
            return ScoreActionResult(0.5, "BASE", "NO_ARTIFACT", None, min_auc, "missing feature columns")
        if features[FEATURE_COLUMNS].isna().any():
            return ScoreActionResult(0.5, "BASE", "NO_ARTIFACT", None, min_auc, "features have NaNs")

        if symbol:
            sym_upper = symbol.upper()
            spec = self._specialists.get(sym_upper)
            if spec is not None:
                # 1. Feature version validation
                import hashlib
                current_hash = hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]
                if spec.get("feature_version") != current_hash:
                    applog.warning(
                        "ai.model",
                        f"[specialist] {sym_upper} feature version mismatch: "
                        f"artifact version {spec.get('feature_version')} != current code version {current_hash} "
                        f"— falling back to global base model",
                    )
                    spec = None
                
                # 2. Check for temporal leakage relative to backtest start time
                elif hasattr(self, "backtest_start_time") and self.backtest_start_time is not None:
                    trained_at_str = spec.get("trained_at")
                    if trained_at_str:
                        trained_at = datetime.fromisoformat(trained_at_str.replace("Z", "+00:00"))
                        bt_start = self.backtest_start_time
                        if bt_start.tzinfo is None:
                            bt_start = bt_start.replace(tzinfo=UTC)
                        if trained_at.tzinfo is None:
                            trained_at = trained_at.replace(tzinfo=UTC)
                        else:
                            trained_at = trained_at.astimezone(UTC)
                        if trained_at > bt_start:
                            applog.warning(
                                "ai.model",
                                f"[specialist] Bypass {sym_upper} due to temporal leakage: "
                                f"model trained_at {trained_at_str} is after backtest start {self.backtest_start_time}",
                            )
                            spec = None

            if spec is not None:
                # Side-specific AUC check
                side_auc = spec.get("auc_short" if action == SignalAction.ENTER_SHORT else "auc_long")
                if side_auc is None:
                    side_auc = spec.get("auc", 0.0)
                auc = side_auc

                # Dynamic safety gate: block if specialist validation AUC is below config threshold
                if auc < min_auc:
                    side_label = "short" if action == SignalAction.ENTER_SHORT else "long"
                    applog.info(
                        "ai.model",
                        f"[specialist] {sym_upper} VALIDATION FAILED ({side_label}) — "
                        f"auc={auc:.4f} < min_auc={min_auc:.4f} | "
                        f"trained_at={spec.get('trained_at', 'unknown')} | "
                        f"n_samples={spec.get('n_samples', 'unknown')} | "
                        f"n_pos={spec.get('n_positive', 'unknown')} | "
                        f"n_neg={spec.get('n_negative', 'unknown')} | "
                        f"feature_version={spec.get('feature_version', 'unknown')} | "
                        f"artifact={self.models_dir / 'specialists' / f'{sym_upper.lower()}.pkl'} "
                        f"— falling back to global base model",
                    )
                    return self._get_base_result(
                        features, action, "REJECTED_LOW_AUC", auc, min_auc,
                        f"Specialist AUC {auc:.4f} below minimum threshold {min_auc:.2f}"
                    )

                # Use the specialist model for inference
                try:
                    x = pd.DataFrame([features[FEATURE_COLUMNS].to_numpy(dtype=float)], columns=FEATURE_COLUMNS)
                    if action == SignalAction.ENTER_SHORT:
                        clf = spec.get("short")
                        if clf is not None:
                            prob = float(predict_proba_safe(clf, x)[0, 1])
                            return ScoreActionResult(prob, "SPECIALIST", "SPECIALIST_USED", auc, min_auc, None)
                        applog.debug("ai.model", f"[specialist] {sym_upper} has no 'short' sub-model — falling back to global")
                    else:
                        clf = spec.get("long")
                        if clf is not None:
                            prob = float(predict_proba_safe(clf, x)[0, 1])
                            return ScoreActionResult(prob, "SPECIALIST", "SPECIALIST_USED", auc, min_auc, None)
                        applog.debug("ai.model", f"[specialist] {sym_upper} has no 'long' sub-model — falling back to global")
                except Exception as exc:
                    applog.debug("ai.model", f"Specialist inference failed for {symbol}: {exc}")
                    return self._get_base_result(features, action, "SPECIALIST_INFERENCE_FAILED", auc, min_auc, str(exc))
            else:
                # No specialist artifact at all for this symbol
                applog.debug(
                    "ai.model",
                    f"[specialist] {sym_upper} no specialist artifact found in "
                    f"{self.models_dir / 'specialists'} — using global model",
                )
                return self._get_base_result(features, action, "NO_ARTIFACT", None, min_auc, None)

        return self._get_base_result(features, action, "NO_SYMBOL", None, min_auc, None)

    def score_action(
        self, features: pd.Series | None, action: SignalAction, symbol: str | None = None
    ) -> float | None:
        """Probability the move is favorable for the proposed action."""
        return self.score_action_detailed(features, action, symbol).probability


    def train_adaptive(
        self,
        frames: dict[str, pd.DataFrame],
        horizon_bars: int = 15,
        threshold_pct: float = 0.10,
        save_to_disk: bool = True,
        regime_key: str | None = None,
    ) -> dict:
        """Fine-tune adaptive model on daily watchlist symbols using warm-start.

        Phase 2: if ``regime_key`` is provided (e.g. ``"trend_up"``, ``"range"``,
        ``"volatile"``), the fine-tuned model is stored as a regime-specific
        adaptive model rather than the global adaptive slot. This means the
        system maintains separate adaptive models for trending vs range-bound
        vs high-volatility market conditions.
        """
        import copy
        from sklearn.ensemble import HistGradientBoostingClassifier
        import joblib

        if not self.is_trained:
            applog.warning("ai.model", "Global model not trained. Adaptive model cannot be fine-tuned; training fresh models instead.")
            new_long = HistGradientBoostingClassifier(max_iter=100, learning_rate=0.06, max_depth=4, early_stopping=True, random_state=42)
            new_short = HistGradientBoostingClassifier(max_iter=100, learning_rate=0.06, max_depth=4, early_stopping=True, random_state=42)
        else:
            new_long = copy.deepcopy(self._model_long)
            try:
                # HistGradientBoostingClassifier uses max_iter
                new_long.set_params(warm_start=True, max_iter=self._model_long.max_iter + 50)
            except AttributeError:
                # LGBMClassifier uses n_estimators
                cur = getattr(self._model_long, "n_estimators", 100)
                new_long.set_params(warm_start=True, n_estimators=cur + 50)
            if self._model_short is not None:
                new_short = copy.deepcopy(self._model_short)
                try:
                    new_short.set_params(warm_start=True, max_iter=self._model_short.max_iter + 50)
                except AttributeError:
                    cur = getattr(self._model_short, "n_estimators", 100)
                    new_short.set_params(warm_start=True, n_estimators=cur + 50)
            else:
                new_short = HistGradientBoostingClassifier(max_iter=100, learning_rate=0.06, max_depth=4, early_stopping=True, random_state=42)

        xs_long, ys_long = [], []
        xs_short, ys_short = [], []

        for symbol, df in frames.items():
            X_l, y_l = build_training_set(df, horizon_bars, threshold_pct, side="long")
            X_s, y_s = build_training_set(df, horizon_bars, threshold_pct, side="short")
            if len(X_l) > 10:
                xs_long.append(X_l)
                ys_long.append(y_l)
            if len(X_s) > 10:
                xs_short.append(X_s)
                ys_short.append(y_s)

        if not xs_long or not xs_short:
            applog.warning("ai.model", "Insufficient daily data for adaptive training. Skipping.")
            return {"status": "skipped", "reason": "insufficient samples"}

        X_l = pd.concat(xs_long)
        y_l = pd.concat(ys_long)
        X_s = pd.concat(xs_short)
        y_s = pd.concat(ys_short)

        new_long.fit(X_l.to_numpy(), y_l.to_numpy())
        new_short.fit(X_s.to_numpy(), y_s.to_numpy())

        if save_to_disk:
            try:
                if regime_key:
                    # Regime-specific adaptive model
                    joblib.dump(new_long, self.models_dir / f"adaptive_{regime_key}_long.joblib")
                    joblib.dump(new_short, self.models_dir / f"adaptive_{regime_key}_short.joblib")
                    self._regime_adaptive_long[regime_key] = new_long
                    self._regime_adaptive_short[regime_key] = new_short
                    applog.info("ai.model", f"Regime-specific adaptive models saved: {regime_key}")
                else:
                    # Global adaptive slot (backward compat)
                    joblib.dump(new_long, self.models_dir / "signal_model_long_adaptive.joblib")
                    joblib.dump(new_short, self.models_dir / "signal_model_short_adaptive.joblib")
                    self._adaptive_long = new_long
                    self._adaptive_short = new_short
                    applog.info("ai.model", "Global adaptive models fine-tuned and saved.")
            except Exception as e:
                applog.error("ai.model", f"Failed to save adaptive model: {e}")
        else:
            if regime_key:
                self._regime_adaptive_long[regime_key] = new_long
                self._regime_adaptive_short[regime_key] = new_short
            else:
                self._adaptive_long = new_long
                self._adaptive_short = new_short

        return {"status": "success", "samples_long": len(X_l), "samples_short": len(X_s),
                "regime_key": regime_key}

    # ---- training ----

    def train(
        self,
        frames: dict[str, pd.DataFrame],
        horizon_bars: int = 15,
        threshold_pct: float = 0.10,
        walk_forward_train_days: int = 60,
        walk_forward_val_days: int = 10,
        walk_forward_min_windows: int = 3,
    ) -> dict:
        """Train long & short models using Walk-Forward Validation.

        Compares LightGBM, XGBoost, and HistGradientBoosting classifiers
        across sliding temporal windows and selects the best classifier per
        side by out-of-sample trading metrics (Profit Factor, Expectancy,
        Sharpe). Falls back to HistGradientBoosting if optional libraries
        are not installed.

        Parameters
        ----------
        frames:
            Dict of symbol -> enriched bar DataFrame.
        horizon_bars:
            Forward-return horizon used to build labels.
        threshold_pct:
            Minimum % price move for a positive label.
        walk_forward_train_days, walk_forward_val_days, walk_forward_min_windows:
            Walk-forward window configuration. These can be overridden at
            call-time from Settings values.
        """
        import joblib
        from sklearn.metrics import roc_auc_score

        wfv = WalkForwardValidator(
            train_days=walk_forward_train_days,
            val_days=walk_forward_val_days,
            min_windows=walk_forward_min_windows,
            horizon_bars=horizon_bars,
            threshold_pct=threshold_pct,
        )

        applog.info("ai.model", "Starting Walk-Forward Validation (long model)...")
        result_long = wfv.run(frames=frames, side="long")
        applog.info("ai.model", "Starting Walk-Forward Validation (short model)...")
        result_short = wfv.run(frames=frames, side="short")

        model_long = result_long.best_classifier
        model_short = result_short.best_classifier

        # ---- Compute holdout AUC for meta-data (last 30% chronologically) ----
        from app.ai.features import build_training_set
        import numpy as np

        def _holdout_auc(clf, all_frames, side):
            xs, ys = [], []
            for df in all_frames.values():
                X, y = build_training_set(df, horizon_bars, threshold_pct, side)
                if len(X) > 50:
                    xs.append(X)
                    ys.append(y)
            if not xs:
                return float("nan")
            X_all = pd.concat(xs).sort_index()
            y_all = pd.concat(ys).sort_index()
            split = int(len(X_all) * 0.70)
            if split < 50 or len(X_all) - split < 20:
                return float("nan")
            proba = clf.predict_proba(X_all.iloc[split:].to_numpy())[:, 1]
            y_val = y_all.iloc[split:]
            if y_val.nunique() < 2:
                return float("nan")
            return float(roc_auc_score(y_val, proba))

        auc_l = _holdout_auc(model_long, frames, "long")
        auc_s = _holdout_auc(model_short, frames, "short")

        # Save dual models
        joblib.dump(model_long, self.models_dir / "signal_model_long.joblib")
        joblib.dump(model_short, self.models_dir / "signal_model_short.joblib")
        # Legacy single-model file for backward compat
        joblib.dump(model_long, self.models_dir / MODEL_FILE)

        self.meta = {
            "trained_at": datetime.now(UTC).isoformat(),
            "engine": f"wfv/{result_long.best_name}+{result_short.best_name}",
            "samples_long": int(sum(len(v) for v in frames.values())),
            "samples_short": int(sum(len(v) for v in frames.values())),
            "validation_auc_long": round(auc_l, 4) if not np.isnan(auc_l) else None,
            "validation_auc_short": round(auc_s, 4) if not np.isnan(auc_s) else None,
            # Walk-forward trading metrics for the selected classifiers
            "wfv_long": result_long.summary.get(result_long.best_name, {}),
            "wfv_short": result_short.summary.get(result_short.best_name, {}),
            "wfv_windows_long": result_long.windows_evaluated,
            "wfv_windows_short": result_short.windows_evaluated,
            "wfv_best_long": result_long.best_name,
            "wfv_best_short": result_short.best_name,
            "horizon_bars": horizon_bars,
            "threshold_pct": threshold_pct,
            "symbols": sorted(frames.keys()),
            "features": FEATURE_COLUMNS,
            # Backward-compat aliases
            "validation_auc": round(auc_l, 4) if not np.isnan(auc_l) else None,
            "samples": int(sum(len(v) for v in frames.values())),   # legacy key
            "validation_accuracy": round(                            # legacy key
                float(sum(
                    m.get("win_rate", 0.5)
                    for m in [result_long.summary.get(result_long.best_name, {})]
                ) / max(1, len([result_long.summary.get(result_long.best_name, {})]))),
                4,
            ),
        }
        (self.models_dir / META_FILE).write_text(json.dumps(self.meta, indent=2))
        self._model_long = model_long
        self._model_short = model_short

        pf_long = result_long.summary.get(result_long.best_name, {}).get("profit_factor", 0)
        pf_short = result_short.summary.get(result_short.best_name, {}).get("profit_factor", 0)
        applog.info(
            "ai.model",
            f"WFV training complete — Long: {result_long.best_name} "
            f"(AUC={self.meta['validation_auc_long']}, PF={pf_long:.3f}), "
            f"Short: {result_short.best_name} "
            f"(AUC={self.meta['validation_auc_short']}, PF={pf_short:.3f})",
        )
        return self.info()
