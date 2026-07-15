from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.generation import AtomicGenerationStore, TrainingBusyError, TrainingCoordinator


class MarkerModel:
    def __init__(self, generation: str, side: str) -> None:
        self.generation = generation
        self.side = side


def test_training_coordinator_claim_is_atomic_before_background_work() -> None:
    coordinator = TrainingCoordinator()
    assert coordinator.try_claim("continuous:a") is True
    assert coordinator.try_claim("continuous:b") is False
    assert coordinator.owner == "continuous:a"
    coordinator.release("continuous:a")
    assert coordinator.try_claim("manual") is True
    coordinator.release("manual")


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


def test_failure_before_manifest_commit_preserves_incumbent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    loaded = AtomicGenerationStore(tmp_path).load()
    assert loaded is not None
    assert loaded.generation_id == incumbent.generation_id
    assert loaded.long_model.generation == loaded.short_model.generation == "g0"


def test_manifest_never_points_at_staging_directory(tmp_path: Path) -> None:
    store = AtomicGenerationStore(tmp_path)
    published = store.publish(MarkerModel("g0", "long"), MarkerModel("g0", "short"), {"name": "g0"})
    manifest = json.loads(store.manifest_path.read_text())
    assert manifest == {"generation_id": published.generation_id}
    assert (store.generations_dir / published.generation_id).is_dir()
    assert not list(store.generations_dir.glob("*.staging"))
