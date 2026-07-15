"""Coordinated production entry point for continuous model training."""

from __future__ import annotations

import asyncio
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import joblib

from app.ai.generation import AtomicSpecialistStore, TrainingCoordinator
from app.ai.trainer_legacy import ContinuousTrainer as _LegacyContinuousTrainer
from app.core.logging import applog


class ContinuousTrainer(_LegacyContinuousTrainer):
    """Continuous trainer whose writers share ownership and atomic publication."""

    def __init__(
        self,
        data,
        model,
        model_dir: Path,
        strategy_overrides: dict[str, dict] | None = None,
        training_coordinator: TrainingCoordinator | None = None,
    ) -> None:
        super().__init__(data, model, model_dir, strategy_overrides)
        coordinator = training_coordinator or getattr(model, "_training_coordinator", None)
        if coordinator is None:
            coordinator = TrainingCoordinator()
            setattr(model, "_training_coordinator", coordinator)
        self.training_coordinator = coordinator
        self._training_owner = f"continuous:{id(self)}"
        self._specialist_store = AtomicSpecialistStore(model_dir)
        self._specialist_fit_lock = threading.Lock()

    async def trigger(self, watchlist: list[str], as_of: datetime | None = None) -> None:
        """Claim synchronously before scheduling so duplicate writers cannot queue."""
        owner = self._training_owner
        if not self.training_coordinator.try_claim(owner):
            applog.info(
                "trainer",
                f"Model training already owned by {self.training_coordinator.owner or 'another writer'} "
                "— skipping continuous trigger.",
            )
            return
        try:
            asyncio.create_task(self._run_cycle(watchlist, as_of, training_owner=owner))
        except BaseException:
            self.training_coordinator.release(owner)
            raise

    async def _run_cycle(
        self,
        watchlist: list[str],
        as_of: datetime | None = None,
        *,
        training_owner: str | None = None,
    ) -> None:
        owner = training_owner
        try:
            await super()._run_cycle(watchlist, as_of)
        finally:
            if owner is not None and self.training_coordinator.owner == owner:
                self.training_coordinator.release(owner)

    def _fit_global_sync(self, dfs):
        """Fit candidates off the inference snapshot, then commit one generation."""
        incumbent = self.model._generation_store.current
        incumbent_long = self.model._model_long
        incumbent_short = self.model._model_short
        incumbent_meta = dict(self.model.meta)
        try:
            auc = super()._fit_global_sync(dfs)
            candidate_long = self.model._model_long
            candidate_short = self.model._model_short
            candidate_meta = dict(self.model.meta)
            changed = (
                candidate_long is not incumbent_long
                or candidate_short is not incumbent_short
                or candidate_meta != incumbent_meta
            )
            if changed:
                snapshot = self.model._generation_store.publish(
                    candidate_long, candidate_short, candidate_meta
                )
                self.model._model_long = snapshot.long_model
                self.model._model_short = snapshot.short_model
                self.model.meta = dict(snapshot.meta)
            return auc
        except BaseException:
            if incumbent is not None:
                self.model._model_long = incumbent.long_model
                self.model._model_short = incumbent.short_model
                self.model.meta = dict(incumbent.meta)
            else:
                self.model._model_long = incumbent_long
                self.model._model_short = incumbent_short
                self.model.meta = incumbent_meta
            raise

    def _fit_specialist_sync(self, symbol, df):
        """Train away from the live path and atomically replace one specialist artifact."""
        with self._specialist_fit_lock:
            live_dir = self.specialist_dir
            with tempfile.TemporaryDirectory(dir=self.model_dir, prefix=".specialist-candidate-") as candidate_root:
                candidate_dir = Path(candidate_root)
                self.specialist_dir = candidate_dir
                try:
                    live_path = live_dir / f"{symbol}.pkl"
                    candidate_path = candidate_dir / f"{symbol}.pkl"
                    if live_path.exists():
                        candidate_path.write_bytes(live_path.read_bytes())
                    result = super()._fit_specialist_sync(symbol, df)
                    if result.get("decision") == "PROMOTED":
                        artifact = joblib.load(candidate_path)
                        self._specialist_store.publish(symbol, artifact)
                        self.model._specialists.pop(symbol.upper(), None)
                    return result
                finally:
                    self.specialist_dir = live_dir
