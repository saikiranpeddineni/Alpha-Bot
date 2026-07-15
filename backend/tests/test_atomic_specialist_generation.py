from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.ai.generation import AtomicSpecialistStore


def test_same_symbol_specialist_publication_is_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import joblib

    store = AtomicSpecialistStore(tmp_path)
    original_dump = joblib.dump
    first_entered = threading.Event()
    release_first = threading.Event()
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def controlled_dump(value, path, *args, **kwargs):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        if value["generation"] == "g0":
            first_entered.set()
            release_first.wait(timeout=2)
        try:
            return original_dump(value, path, *args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(joblib, "dump", controlled_dump)
    first = threading.Thread(target=store.publish, args=("aapl", {"generation": "g0"}))
    second = threading.Thread(target=store.publish, args=("AAPL", {"generation": "g1"}))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert max_active == 1
    assert joblib.load(tmp_path / "specialists" / "AAPL.pkl") == {"generation": "g1"}
    assert not list((tmp_path / "specialists").glob(".*.tmp"))


def test_failed_specialist_publication_preserves_incumbent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import joblib

    store = AtomicSpecialistStore(tmp_path)
    target = store.publish("MSFT", {"generation": "g0"})
    original_dump = joblib.dump

    def fail_after_dump(value, path, *args, **kwargs):
        result = original_dump(value, path, *args, **kwargs)
        raise OSError("injected specialist persistence failure")

    monkeypatch.setattr(joblib, "dump", fail_after_dump)
    with pytest.raises(OSError, match="injected specialist persistence failure"):
        store.publish("MSFT", {"generation": "g1"})

    assert joblib.load(target) == {"generation": "g0"}
    assert not list((tmp_path / "specialists").glob(".*.tmp"))
