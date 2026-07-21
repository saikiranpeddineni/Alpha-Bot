import pytest
from app.config import RiskConfig
from app.core.promotion import (
    PolicyPromotionGovernor,
    PromotionEvidenceEnvelope,
    SafetyCheckEvidence,
    SafetyCheckStatus,
)
from app.db.database import Database


def test_safety_check_status_enum():
    """Test #247: Safety check status enum enforces PASS, FAIL, ERROR, NOT_RUN, STALE."""
    assert SafetyCheckStatus.PASS == "PASS"
    assert SafetyCheckStatus.FAIL == "FAIL"
    assert SafetyCheckStatus.ERROR == "ERROR"
    assert SafetyCheckStatus.NOT_RUN == "NOT_RUN"
    assert SafetyCheckStatus.STALE == "STALE"


def test_immutable_evidence_envelope_signature():
    """Test #251: Evidence envelope signature verification fails if payload is tampered."""
    check_pass = SafetyCheckEvidence(
        rule_name="min_observations",
        status=SafetyCheckStatus.PASS,
        details="35 >= 30 valid observations"
    )
    envelope = PromotionEvidenceEnvelope(
        policy_id="policy_alpha",
        git_commit_sha="e4d0891",
        package_hash="abc123hash",
        safety_checks=[check_pass],
        canonical_evidence_metadata={"dataset_window_id": "win_1"},
    )
    assert envelope.verify_integrity() is True

    # Tamper with policy_id
    envelope.policy_id = "policy_tampered"
    assert envelope.verify_integrity() is False


def test_configurable_promotion_criteria():
    """Test #249: Promotion criteria parameters are configurable in RiskConfig."""
    cfg = RiskConfig(
        promotion_min_valid_observations=50,
        promotion_min_win_rate_pct=55.0,
        promotion_min_expectancy_r=0.2
    )
    assert cfg.promotion_min_valid_observations == 50
    assert cfg.promotion_min_win_rate_pct == 55.0
    assert cfg.promotion_min_expectancy_r == 0.2


def test_fail_closed_atomic_promotion(tmp_path):
    """Test #246: Fail-closed atomic promotion transaction persists snapshot and evidence or fails closed."""
    db_file = tmp_path / "test_batch_d.db"
    db = Database(db_file)
    governor = PolicyPromotionGovernor(db=db)

    check_pass = SafetyCheckEvidence(
        rule_name="min_observations",
        status=SafetyCheckStatus.PASS,
        details="35 >= 30 valid observations"
    )
    envelope = PromotionEvidenceEnvelope(
        policy_id="policy_champion_v2",
        git_commit_sha="161e30f",
        package_hash="hash_pack_123",
        safety_checks=[check_pass],
        canonical_evidence_metadata={"dataset_window_id": "window_2026_07"},
    )

    snapshot = {"ml_threshold": 0.58, "policy_id": "policy_champion_v2"}
    res = governor.execute_atomic_promotion(envelope, snapshot)
    assert res["status"] == "PROMOTED"

    # Verify setting was saved
    saved_snapshot = db.get_setting("active_policy_snapshot")
    assert saved_snapshot is not None
    assert saved_snapshot["ml_threshold"] == 0.58

    # Failed safety check fails closed
    check_fail = SafetyCheckEvidence(
        rule_name="adversarial_robustness",
        status=SafetyCheckStatus.FAIL,
        details="Adversarial stress test failed"
    )
    envelope_failed = PromotionEvidenceEnvelope(
        policy_id="policy_bad",
        git_commit_sha="161e30f",
        package_hash="hash_bad",
        safety_checks=[check_fail],
        canonical_evidence_metadata={},
    )
    with pytest.raises(ValueError, match="safety checks failed"):
        governor.execute_atomic_promotion(envelope_failed, snapshot)
