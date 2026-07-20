import hashlib
from pathlib import Path
from app.backtest.engine import compute_decision_artifacts_manifest, BacktestParams, BacktestResult


def test_decision_artifacts_manifest_fingerprint(tmp_path):
    """Verify that changing any model file, policy, or challenger changes decision_artifacts_sha256."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()

    # Create dummy base model files
    (models_dir / "signal_model_long.joblib").write_bytes(b"long_v1")
    (models_dir / "signal_model_short.joblib").write_bytes(b"short_v1")

    active_policy = {"version": "v1.0", "ml_threshold": 0.52}
    challengers = [{"policy_id": "c1", "threshold": 0.55}]

    manifest1, sha1 = compute_decision_artifacts_manifest(
        models_dir, active_policy=active_policy, challenger_policies=challengers
    )

    assert "signal_model_long.joblib" in manifest1["base_models"]
    assert sha1 != ""

    # 1. Modify active policy -> sha must change
    active_policy2 = {"version": "v1.0", "ml_threshold": 0.58}
    _, sha2 = compute_decision_artifacts_manifest(
        models_dir, active_policy=active_policy2, challenger_policies=challengers
    )
    assert sha1 != sha2

    # 2. Add specialist model -> sha must change
    (models_dir / "specialist_tech.joblib").write_bytes(b"spec_v1")
    manifest3, sha3 = compute_decision_artifacts_manifest(
        models_dir, active_policy=active_policy, challenger_policies=challengers
    )
    assert "specialist_tech.joblib" in manifest3["specialist_models"]
    assert sha1 != sha3


def test_backtest_params_model_eligibility_mode():
    params = BacktestParams(
        symbols=["AVGO"],
        start="2026-07-01T00:00:00Z",
        end="2026-07-05T00:00:00Z",
    )
    assert params.model_eligibility_mode == "walk_forward"
