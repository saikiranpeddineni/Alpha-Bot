from pathlib import Path

from app.strategies.pipeline import SignalPipeline


def test_signal_pipeline_has_no_target_d_runtime_dependency():
    source = Path(__file__).parents[1] / "app" / "strategies" / "pipeline.py"
    text = source.read_text(encoding="utf-8")
    assert "TargetDShadowObserver" not in text
    assert "target_d_observer" not in text
    assert "target_d_probability" not in text
    assert "target_d_gate_decision" not in text
    assert "target_d_disagreement" not in text


def test_pipeline_instance_exposes_no_shadow_challenger(monkeypatch):
    # Constructor inspection is enough to guard the architectural boundary without
    # loading model artifacts or invoking any challenger.
    names = set(SignalPipeline.__init__.__code__.co_names)
    assert "target_d_observer" not in names
    assert "TargetDShadowObserver" not in names
