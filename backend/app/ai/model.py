"""Atomic generation facade for the production signal model."""

from __future__ import annotations

import pandas as pd

from app.ai.generation import AtomicGenerationStore, TrainingCoordinator
from app.ai.model_legacy import *  # noqa: F403
from app.ai.model_legacy import SignalModel as _LegacySignalModel


class SignalModel(_LegacySignalModel):
    """Signal model with one writer coordinator and manifest-authoritative globals."""

    def __init__(self, models_dir):
        self._training_coordinator = TrainingCoordinator()
        self._generation_store = AtomicGenerationStore(models_dir)
        super().__init__(models_dir)

    def _load(self) -> None:
        committed = self._generation_store.load()
        if committed is None:
            super()._load()
            return
        self._model_long = committed.long_model
        self._model_short = committed.short_model
        self.meta = dict(committed.meta)
        # Legacy loader also owns adaptive and specialist artifact discovery.
        global_paths = (
            self.models_dir / "signal_model_long.joblib",
            self.models_dir / "signal_model_short.joblib",
            self.models_dir / "signal_model.joblib",
            self.models_dir / META_FILE,  # noqa: F405
        )
        hidden = []
        try:
            for path in global_paths:
                if path.exists():
                    tmp = path.with_name(f".{path.name}.generation-load")
                    path.replace(tmp)
                    hidden.append((path, tmp))
            super()._load()
        finally:
            for path, tmp in reversed(hidden):
                tmp.replace(path)
        self._model_long = committed.long_model
        self._model_short = committed.short_model
        self.meta = dict(committed.meta)

    def p_up(self, features, regime_key=None) -> float:
        snapshot = self._generation_store.current
        global_long = snapshot.long_model if snapshot is not None else self._model_long
        if global_long is None:
            return self._heuristic_p_up(features)
        x = pd.DataFrame([features[FEATURE_COLUMNS].to_numpy(dtype=float)], columns=FEATURE_COLUMNS)  # noqa: F405
        p_global = float(predict_proba_safe(global_long, x)[0, 1])  # noqa: F405
        reg_model = self._regime_adaptive_long.get(regime_key or "")
        if reg_model is not None:
            p_adaptive = float(predict_proba_safe(reg_model, x)[0, 1])  # noqa: F405
            return 0.70 * p_global + 0.30 * p_adaptive
        if self._adaptive_long is not None:
            p_adaptive = float(predict_proba_safe(self._adaptive_long, x)[0, 1])  # noqa: F405
            return 0.70 * p_global + 0.30 * p_adaptive
        return p_global

    def p_down(self, features, regime_key=None) -> float:
        snapshot = self._generation_store.current
        global_short = snapshot.short_model if snapshot is not None else self._model_short
        if global_short is None:
            return 1.0 - self.p_up(features)
        x = pd.DataFrame([features[FEATURE_COLUMNS].to_numpy(dtype=float)], columns=FEATURE_COLUMNS)  # noqa: F405
        p_global = float(predict_proba_safe(global_short, x)[0, 1])  # noqa: F405
        reg_model = self._regime_adaptive_short.get(regime_key or "")
        if reg_model is not None:
            p_adaptive = float(predict_proba_safe(reg_model, x)[0, 1])  # noqa: F405
            return 0.70 * p_global + 0.30 * p_adaptive
        if self._adaptive_short is not None:
            p_adaptive = float(predict_proba_safe(self._adaptive_short, x)[0, 1])  # noqa: F405
            return 0.70 * p_global + 0.30 * p_adaptive
        return p_global

    def train(self, *args, **kwargs) -> dict:
        with self._training_coordinator.claim_sync(f"manual:{id(self)}"):
            incumbent = self._generation_store.current
            try:
                info = super().train(*args, **kwargs)
                snapshot = self._generation_store.publish(self._model_long, self._model_short, dict(self.meta))
            except BaseException:
                if incumbent is not None:
                    self._model_long = incumbent.long_model
                    self._model_short = incumbent.short_model
                    self.meta = dict(incumbent.meta)
                raise
            self._model_long = snapshot.long_model
            self._model_short = snapshot.short_model
            self.meta = dict(snapshot.meta)
            return {**info, **self.info()}

    def train_adaptive(self, *args, **kwargs) -> dict:
        with self._training_coordinator.claim_sync(f"adaptive:{id(self)}"):
            return super().train_adaptive(*args, **kwargs)
