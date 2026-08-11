from app.config import RiskConfig
from app.config import Settings
from pathlib import Path

import joblib

from app.ai.generation import TrainingBusyError
from app.ai.model import SignalModel


class Marker:
    def __init__(self, generation):
        self.generation = generation


def test_restart_prefers_committed_manifest_over_mixed_legacy_files(tmp_path: Path) -> None:
    model = SignalModel(tmp_path, risk_config=RiskConfig())
    committed = model._generation_store.publish(Marker("g0"), Marker("g0"), {"trained_at": "g0"})
    joblib.dump(Marker("mixed-long"), tmp_path / "signal_model_long.joblib")
    joblib.dump(Marker("mixed-short"), tmp_path / "signal_model_short.joblib")
    (tmp_path / "signal_model.meta.json").write_text('{"trained_at":"mixed"}')

    restarted = SignalModel(tmp_path, risk_config=RiskConfig())
    assert restarted._generation_store.current.generation_id == committed.generation_id
    assert restarted._model_long.generation == restarted._model_short.generation == "g0"
    assert restarted.meta["trained_at"] == "g0"


def test_manual_and_adaptive_writers_share_one_coordinator(tmp_path: Path) -> None:
    model = SignalModel(tmp_path, risk_config=RiskConfig())
    assert model._training_coordinator.try_claim("continuous") is True
    try:
        try:
            model.train({})
        except TrainingBusyError:
            pass
        else:
            raise AssertionError("manual writer bypassed shared coordinator")
        try:
            model.train_adaptive({})
        except TrainingBusyError:
            pass
        else:
            raise AssertionError("adaptive writer bypassed shared coordinator")
    finally:
        model._training_coordinator.release("continuous")
