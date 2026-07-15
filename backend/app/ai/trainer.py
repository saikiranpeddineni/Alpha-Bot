"""Coordinated production entry point for continuous model training.

The implementation remains in trainer_legacy while this facade owns the shared
writer slot before scheduling. This keeps the production import stable and makes
same-event-loop trigger admission atomic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from app.ai.generation import TrainingCoordinator
from app.ai.trainer_legacy import ContinuousTrainer as _LegacyContinuousTrainer
from app.core.logging import applog


class ContinuousTrainer(_LegacyContinuousTrainer):
    """Continuous trainer whose trigger participates in shared writer ownership."""

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
