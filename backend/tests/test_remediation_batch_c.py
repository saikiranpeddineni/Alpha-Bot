import pytest
from app.core.canonical_evidence import CanonicalOutcomeResolver
from app.core.cohort_fingerprint import generate_cohort_fingerprint
from app.db.database import Database


def test_deterministic_cohort_fingerprint():
    """Test #253: Cohort fingerprint generation is deterministic and SHA-256 bound."""
    manifest1 = [
        {"policy_id": "pol_1", "params": {"atr": 1.5, "tp": 2.0}},
        {"policy_id": "pol_2", "params": {"atr": 2.0, "tp": 3.0}},
    ]
    manifest2 = [
        {"policy_id": "pol_1", "params": {"tp": 2.0, "atr": 1.5}},
        {"policy_id": "pol_2", "params": {"tp": 3.0, "atr": 2.0}},
    ]
    fp1 = generate_cohort_fingerprint(manifest1)
    fp2 = generate_cohort_fingerprint(manifest2)

    assert fp1 == fp2
    assert len(fp1) == 64  # SHA-256 hex digest length


def test_atomic_candidate_database_update(tmp_path):
    """Test #242: Targeted SQL update asserts affected_rows == 1."""
    db_file = tmp_path / "test_batch_c.db"
    db = Database(db_file)

    # Save a initial shadow record
    db.save_shadow_signal(
        "cand_atomic",
        None,
        "NVDA",
        "rsi2",
        "long",
        "demo",
        {"candidate_id": "cand_atomic", "policy_id": "challenger_1", "final_decision": "rejected"}
    )

    # Update atomic should succeed for existing row
    updated = db.update_shadow_signal_atomic(
        "cand_atomic",
        "challenger_1",
        {"final_decision": "pipeline_approved", "rejection_reason": None}
    )
    assert updated is True

    # Asserting non-existent row raises ValueError due to affected_rows == 0
    with pytest.raises(ValueError, match="affected_rows=0"):
        db.update_shadow_signal_atomic(
            "non_existent_cand",
            "challenger_1",
            {"final_decision": "pipeline_approved"}
        )


def test_nomination_denominator_matching():
    """Test #237: Nomination promotion denominator requires matched valid observation pairs >= MIN_PROMOTION_SAMPLE."""
    champ_recs = [
        {"candidate_id": f"c_{i}", "final_decision": "rejected", "replayed": True, "realized_r": 1.0, "stop_loss": 90, "entry_price": 100}
        for i in range(35)
    ]
    cand_recs = [
        {"candidate_id": f"c_{i}", "final_decision": "rejected", "replayed": True, "realized_r": 1.5, "stop_loss": 90, "entry_price": 100}
        for i in range(35)
    ]

    res = CanonicalOutcomeResolver.calculate_matched_nomination_evidence(
        champ_recs, cand_recs, min_promotion_sample=30
    )
    assert res["matched_valid_count"] == 35
    assert res["nomination_eligible"] is True

    # Insufficient matched samples test
    res_small = CanonicalOutcomeResolver.calculate_matched_nomination_evidence(
        champ_recs[:10], cand_recs[:10], min_promotion_sample=30
    )
    assert res_small["matched_valid_count"] == 10
    assert res_small["nomination_eligible"] is False
    assert res_small["reason"] == "INSUFFICIENT_MATCHED_SAMPLES"
