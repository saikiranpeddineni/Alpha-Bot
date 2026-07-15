"""Serialized training ownership and atomic global-model generation publication."""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


MANIFEST_FILE = "signal_model.current.json"
GENERATIONS_DIR = "generations"


class TrainingBusyError(RuntimeError):
    """Raised when another writer already owns the model training slot."""


class TrainingCoordinator:
    """One ownership primitive shared by async entry points and worker threads.

    Ownership is claimed synchronously before an async caller can schedule or
    yield. That closes the same-event-loop-turn race where two trigger calls
    both observe an idle boolean and both enqueue writers.
    """

    def __init__(self) -> None:
        self._gate = threading.Lock()
        self._owner_lock = threading.Lock()
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        with self._owner_lock:
            return self._owner

    @property
    def busy(self) -> bool:
        return self.owner is not None

    def try_claim(self, owner: str) -> bool:
        if not self._gate.acquire(blocking=False):
            return False
        with self._owner_lock:
            self._owner = owner
        return True

    def release(self, owner: str) -> None:
        with self._owner_lock:
            if self._owner != owner:
                raise RuntimeError(f"training slot owned by {self._owner!r}, not {owner!r}")
            self._owner = None
        self._gate.release()

    @contextmanager
    def claim_sync(self, owner: str) -> Iterator[None]:
        if not self.try_claim(owner):
            raise TrainingBusyError(f"model training already owned by {self.owner or 'another writer'}")
        try:
            yield
        finally:
            self.release(owner)

    @asynccontextmanager
    async def claim(self, owner: str):
        if not self.try_claim(owner):
            raise TrainingBusyError(f"model training already owned by {self.owner or 'another writer'}")
        try:
            yield
        finally:
            self.release(owner)


@dataclass(frozen=True)
class ModelGeneration:
    """Immutable inference snapshot: long, short, and metadata from one commit."""

    generation_id: str
    long_model: object
    short_model: object
    meta: dict


class AtomicGenerationStore:
    """Publish immutable generation directories through one atomic manifest swap."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self.generations_dir = models_dir / GENERATIONS_DIR
        self.manifest_path = models_dir / MANIFEST_FILE
        self.generations_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_lock = threading.Lock()
        self._snapshot: ModelGeneration | None = None

    @property
    def current(self) -> ModelGeneration | None:
        """Return the already-committed in-memory snapshot without disk mixing."""
        with self._snapshot_lock:
            return self._snapshot

    def load(self) -> ModelGeneration | None:
        """Load exactly the generation named by one manifest read."""
        if not self.manifest_path.exists():
            return None
        import joblib

        manifest = json.loads(self.manifest_path.read_text())
        generation_id = str(manifest["generation_id"])
        generation_dir = self.generations_dir / generation_id
        meta = json.loads((generation_dir / "meta.json").read_text())
        if meta.get("generation_id") != generation_id:
            raise ValueError(
                f"generation metadata mismatch: manifest={generation_id} "
                f"meta={meta.get('generation_id')}"
            )
        snapshot = ModelGeneration(
            generation_id=generation_id,
            long_model=joblib.load(generation_dir / "long.joblib"),
            short_model=joblib.load(generation_dir / "short.joblib"),
            meta=meta,
        )
        with self._snapshot_lock:
            self._snapshot = snapshot
        return snapshot

    def publish(self, long_model: object, short_model: object, meta: dict) -> ModelGeneration:
        """Commit a complete generation, then swap memory to that same snapshot."""
        import joblib

        generation_id = uuid.uuid4().hex
        staging = self.generations_dir / f".{generation_id}.staging"
        committed = self.generations_dir / generation_id
        manifest_tmp = self.models_dir / f".{MANIFEST_FILE}.{generation_id}.tmp"
        staging.mkdir(parents=False, exist_ok=False)
        generation_meta = {**meta, "generation_id": generation_id}
        snapshot = ModelGeneration(generation_id, long_model, short_model, generation_meta)
        try:
            joblib.dump(long_model, staging / "long.joblib")
            joblib.dump(short_model, staging / "short.joblib")
            (staging / "meta.json").write_text(json.dumps(generation_meta, indent=2))
            os.replace(staging, committed)
            manifest_tmp.write_text(json.dumps({"generation_id": generation_id}))
            os.replace(manifest_tmp, self.manifest_path)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            manifest_tmp.unlink(missing_ok=True)
            raise

        # In-memory inference changes only after the durable commit point.
        with self._snapshot_lock:
            self._snapshot = snapshot
        return snapshot
