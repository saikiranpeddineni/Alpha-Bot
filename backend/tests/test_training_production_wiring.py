from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.generation import TrainingCoordinator
from app.ai.trainer import ContinuousTrainer


class _Data:
    pass


class _Model:
    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self.meta = {}
        self._specialists = {}


def test_continuous_trigger_claims_shared_slot_before_scheduling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        coordinator = TrainingCoordinator()
        trainer = ContinuousTrainer(_Data(), _Model(tmp_path), tmp_path, training_coordinator=coordinator)
        entered = asyncio.Event()
        release = asyncio.Event()
        runs = 0

        async def controlled_cycle(watchlist, as_of=None, *, training_owner=None):
            nonlocal runs
            runs += 1
            entered.set()
            await release.wait()
            coordinator.release(training_owner)

        monkeypatch.setattr(trainer, "_run_cycle", controlled_cycle)
        await asyncio.gather(trainer.trigger(["AAPL"]), trainer.trigger(["MSFT"]))
        await entered.wait()
        assert runs == 1
        assert coordinator.busy is True
        release.set()
        await asyncio.sleep(0)
        assert coordinator.busy is False

    asyncio.run(scenario())


def test_continuous_trigger_rejects_when_manual_writer_owns_shared_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        coordinator = TrainingCoordinator()
        trainer = ContinuousTrainer(_Data(), _Model(tmp_path), tmp_path, training_coordinator=coordinator)
        scheduled = False

        async def forbidden_cycle(*args, **kwargs):
            nonlocal scheduled
            scheduled = True

        monkeypatch.setattr(trainer, "_run_cycle", forbidden_cycle)
        assert coordinator.try_claim("manual") is True
        try:
            await trainer.trigger(["AAPL"])
            await asyncio.sleep(0)
            assert scheduled is False
            assert coordinator.owner == "manual"
        finally:
            coordinator.release("manual")

    asyncio.run(scenario())
