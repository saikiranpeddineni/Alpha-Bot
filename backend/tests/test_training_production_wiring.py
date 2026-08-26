from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.ai.generation import AtomicGenerationStore, TrainingCoordinator
from app.ai.trainer import ContinuousTrainer, _LegacyContinuousTrainer
from app.data.base import HistoricalDataCoverageError


class _Data:
    pass


class _Model:
    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self.meta = {}
        self._specialists = {}


class _Marker:
    def __init__(self, generation: str) -> None:
        self.generation = generation


class _AtomicModel(_Model):
    def __init__(self, models_dir: Path) -> None:
        super().__init__(models_dir)
        self._generation_store = AtomicGenerationStore(models_dir)
        snapshot = self._generation_store.publish(_Marker("g0"), _Marker("g0"), {"name": "g0"})
        self._model_long = snapshot.long_model
        self._model_short = snapshot.short_model
        self.meta = dict(snapshot.meta)


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


def test_continuous_global_promotion_commits_one_complete_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = _AtomicModel(tmp_path)
    trainer = ContinuousTrainer(_Data(), model, tmp_path)

    def legacy_fit(self, dfs):
        self.model._model_long = _Marker("g1")
        self.model._model_short = _Marker("g1")
        self.model.meta = {"name": "g1"}
        return 0.9

    monkeypatch.setattr(_LegacyContinuousTrainer, "_fit_global_sync", legacy_fit)
    assert trainer._fit_global_sync({}) == 0.9
    snapshot = model._generation_store.current
    assert snapshot.long_model.generation == snapshot.short_model.generation == "g1"
    assert snapshot.meta["name"] == "g1"
    restarted = AtomicGenerationStore(tmp_path).load()
    assert restarted.long_model.generation == restarted.short_model.generation == "g1"


def test_cancelled_continuous_global_fit_restores_incumbent_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = _AtomicModel(tmp_path)
    trainer = ContinuousTrainer(_Data(), model, tmp_path)
    incumbent = model._generation_store.current

    def failing_fit(self, dfs):
        self.model._model_long = _Marker("partial")
        self.model.meta = {"name": "partial"}
        raise asyncio.CancelledError()

    monkeypatch.setattr(_LegacyContinuousTrainer, "_fit_global_sync", failing_fit)
    with pytest.raises(asyncio.CancelledError):
        trainer._fit_global_sync({})
    assert model._generation_store.current.generation_id == incumbent.generation_id
    assert model._model_long.generation == model._model_short.generation == "g0"
    restarted = AtomicGenerationStore(tmp_path).load()
    assert restarted.generation_id == incumbent.generation_id


class _CoverageRaisingCache:
    """Cache-backed history that always fails the immutable coverage check."""

    def __init__(self, provider_df: pd.DataFrame | None, calls: list[bool]) -> None:
        self.provider_df = provider_df
        self.calls = calls

    async def history(self, symbol, timeframe, start, end, prefer_cache=True, cache_policy=None):
        self.calls.append(prefer_cache)
        if prefer_cache:
            raise HistoricalDataCoverageError(
                symbol=symbol, timeframe=timeframe, start=start, end=end,
                cached_start=None, cached_end=None,
                reason="Cached 1-minute session is materially partial",
            )
        return self.provider_df


def _minute_frame(n: int) -> pd.DataFrame:
    idx = pd.date_range("2026-08-01 13:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 10}, index=idx)


def test_fetch_all_repairs_from_provider_when_coverage_errors_exhaust_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-08-26: a holey recent session raised HistoricalDataCoverageError at
    every ladder rung; the final-rung repair only handled short frames, so the
    whole cycle aborted with 'No usable data fetched' and models went stale."""
    async def scenario() -> None:
        trainer = ContinuousTrainer(_Data(), _Model(tmp_path), tmp_path)
        calls: list[bool] = []
        provider_df = _minute_frame(200)
        trainer.data = _CoverageRaisingCache(provider_df, calls)
        monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(train_cache_only=True))

        dfs = await trainer._fetch_all(["AAPL"], datetime.now(UTC) - timedelta(days=180), datetime.now(UTC))

        assert dfs.get("AAPL") is provider_df
        assert len(calls) > 1
        assert all(calls[:-1])         # every ladder rung tried the cache
        assert calls[-1] is False      # final attempt repaired from provider

    asyncio.run(scenario())


def test_fetch_all_drops_symbol_when_provider_repair_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        trainer = ContinuousTrainer(_Data(), _Model(tmp_path), tmp_path)
        calls: list[bool] = []
        trainer.data = _CoverageRaisingCache(None, calls)
        monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(train_cache_only=True))

        dfs = await trainer._fetch_all(["AAPL"], datetime.now(UTC) - timedelta(days=180), datetime.now(UTC))

        assert "AAPL" not in dfs
        assert calls[-1] is False

    asyncio.run(scenario())
