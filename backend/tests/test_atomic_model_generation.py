from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from app.ai.generation import AtomicGenerationStore, TrainingBusyError, TrainingCoordinator


class MarkerModel:
    def __init__(self, generation: str, side: str) -> None:
        self.generation = generation
        self.side = side


def _assert_incumbent(store: AtomicGenerationStore, generation_id: str) -> None:
    assert store.current is not None
    assert store.current.generation_id == generation_id
    assert store.current.long_model.generation == store.current.short_model.generation == "g0"
    loaded = AtomicGenerationStore(store.models_dir).load()
    assert loaded is not None
    assert loaded.generation_id == generation_id
    assert loaded.long_model.generation == loaded.short_model.generation == "g0"


def test_training_coordinator_claim_is_atomic_before_background_work() -> None:
    coordinator = TrainingCoordinator()
    assert coordinator.try_claim("continuous:a") is True
    assert coordinator.try_claim("continuous:b") is False
    assert coordinator.owner == "continuous:a"
    coordinator.release("continuous:a")
    assert coordinator.try_claim("manual") is True
    coordinator.release("manual")


def test_same_event_loop_turn_allows_exactly_one_training_owner() -> None:
    async def scenario() -> None:
        coordinator = TrainingCoordinator()
        entered: list[str] = []
        release = asyncio.Event()

        async def trigger(owner: str) -> bool:
            if not coordinator.try_claim(owner):
                return False
            entered.append(owner)
            try:
                await release.wait()
            finally:
                coordinator.release(owner)
            return True

        first = asyncio.create_task(trigger("continuous:first"))
        second = asyncio.create_task(trigger("continuous:second"))
        await asyncio.sleep(0)
        assert len(entered) == 1
        assert coordinator.busy is True
        release.set()
        assert sum(await asyncio.gather(first, second)) == 1
        assert coordinator.busy is False

    asyncio.run(scenario())


def test_forced_thread_interleaving_admits_one_writer() -> None:
    coordinator = TrainingCoordinator()
    barrier = threading.Barrier(3)
    release = threading.Event()
    results: list[tuple[str, bool]] = []
    results_lock = threading.Lock()

    def writer(owner: str) -> None:
        barrier.wait()
        claimed = coordinator.try_claim(owner)
        with results_lock:
            results.append((owner, claimed))
        if claimed:
            release.wait(timeout=2)
            coordinator.release(owner)

    threads = [threading.Thread(target=writer, args=("manual",)), threading.Thread(target=writer, args=("continuous",))]
    for thread in threads:
        thread.start()
    barrier.wait()
    while len(results) < 2:
        pass
    assert sum(claimed for _, claimed in results) == 1
    release.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert coordinator.busy is False


def test_reverse_writer_order_is_rejected_deterministically() -> None:
    coordinator = TrainingCoordinator()
    with coordinator.claim_sync("manual"):
        with pytest.raises(TrainingBusyError):
            with coordinator.claim_sync("continuous"):
                pass
    with coordinator.claim_sync("continuous"):
        with pytest.raises(TrainingBusyError):
            with coordinator.claim_sync("adaptive"):
                pass


def test_atomic_store_restart_loads_one_complete_generation(tmp_path: Path) -> None:
    store = AtomicGenerationStore(tmp_path)
    g0 = store.publish(MarkerModel("g0", "long"), MarkerModel("g0", "short"), {"name": "g0"})
    loaded0 = AtomicGenerationStore(tmp_path).load()
    assert loaded0 is not None
    assert loaded0.generation_id == g0.generation_id
    assert loaded0.long_model.generation == loaded0.short_model.generation == "g0"
    assert loaded0.meta["generation_id"] == g0.generation_id

    g1 = store.publish(MarkerModel("g1", "long"), MarkerModel("g1", "short"), {"name": "g1"})
    loaded1 = AtomicGenerationStore(tmp_path).load()
    assert loaded1 is not None
    assert loaded1.generation_id == g1.generation_id
    assert loaded1.long_model.generation == loaded1.short_model.generation == "g1"
    assert loaded1.meta["generation_id"] == g1.generation_id


def test_inference_snapshot_never_mixes_sides_during_publish(tmp_path: Path) -> None:
    store = AtomicGenerationStore(tmp_path)
    store.publish(MarkerModel("g0", "long"), MarkerModel("g0", "short"), {"name": "g0"})
    before = store.current
    assert before is not None
    store.publish(MarkerModel("g1", "long"), MarkerModel("g1", "short"), {"name": "g1"})
    after = store.current
    assert after is not None
    assert before.long_model.generation == before.short_model.generation == "g0"
    assert after.long_model.generation == after.short_model.generation == "g1"
    assert before.generation_id != after.generation_id


@pytest.mark.parametrize("fail_dump_number", [1, 2])
def test_failure_after_each_candidate_persistence_step_preserves_incumbent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_dump_number: int
) -> None:
    store = AtomicGenerationStore(tmp_path)
    incumbent = store.publish(MarkerModel("g0", "long"), MarkerModel("g0", "short"), {"name": "g0"})
    import joblib

    original_dump = joblib.dump
    calls = 0

    def fail_selected_dump(value, path, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_dump(value, path, *args, **kwargs)
        if calls == fail_dump_number:
            raise OSError(f"injected persistence failure {fail_dump_number}")
        return result

    monkeypatch.setattr(joblib, "dump", fail_selected_dump)
    with pytest.raises(OSError, match="injected persistence failure"):
        store.publish(MarkerModel("g1", "long"), MarkerModel("g1", "short"), {"name": "g1"})
    _assert_incumbent(store, incumbent.generation_id)
    assert not list(store.generations_dir.glob("*.staging"))


def test_failure_before_manifest_commit_preserves_disk_and_memory_incumbent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AtomicGenerationStore(tmp_path)
    incumbent = store.publish(MarkerModel("g0", "long"), MarkerModel("g0", "short"), {"name": "g0"})
    original_replace = __import__("app.ai.generation", fromlist=["os"]).os.replace

    def fail_manifest_swap(src, dst):
        if Path(dst) == store.manifest_path:
            raise OSError("injected manifest failure")
        return original_replace(src, dst)

    monkeypatch.setattr("app.ai.generation.os.replace", fail_manifest_swap)
    with pytest.raises(OSError, match="injected manifest failure"):
        store.publish(MarkerModel("g1", "long"), MarkerModel("g1", "short"), {"name": "g1"})
    _assert_incumbent(store, incumbent.generation_id)


def test_cancellation_during_candidate_persistence_cannot_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AtomicGenerationStore(tmp_path)
    incumbent = store.publish(MarkerModel("g0", "long"), MarkerModel("g0", "short"), {"name": "g0"})
    import joblib

    original_dump = joblib.dump
    calls = 0

    def cancel_after_long(value, path, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_dump(value, path, *args, **kwargs)
        if calls == 1:
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(joblib, "dump", cancel_after_long)
    with pytest.raises(asyncio.CancelledError):
        store.publish(MarkerModel("g1", "long"), MarkerModel("g1", "short"), {"name": "g1"})
    _assert_incumbent(store, incumbent.generation_id)
    assert not list(store.generations_dir.glob("*.staging"))


def test_restart_rejects_manifest_metadata_generation_mismatch(tmp_path: Path) -> None:
    store = AtomicGenerationStore(tmp_path)
    published = store.publish(MarkerModel("g0", "long"), MarkerModel("g0", "short"), {"name": "g0"})
    meta_path = store.generations_dir / published.generation_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["generation_id"] = "different-generation"
    meta_path.write_text(json.dumps(meta))
    # Hardened contract: a generation whose metadata mismatches its manifest is
    # treated as absent so restart fails closed to rule-based gating instead of
    # crashing startup.
    fresh_store = AtomicGenerationStore(tmp_path)
    assert fresh_store.load() is None
    assert fresh_store.current is None


def test_manifest_never_points_at_staging_directory(tmp_path: Path) -> None:
    store = AtomicGenerationStore(tmp_path)
    published = store.publish(MarkerModel("g0", "long"), MarkerModel("g0", "short"), {"name": "g0"})
    manifest = json.loads(store.manifest_path.read_text())
    assert manifest == {"generation_id": published.generation_id}
    assert (store.generations_dir / published.generation_id).is_dir()
    assert not list(store.generations_dir.glob("*.staging"))
