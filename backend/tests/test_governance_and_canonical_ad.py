"""Authoritative Regression and Adversarial Governance Test Suite for A-D Backlog (#224, #234, #238, #244, #237, #253, #254, #248, #249, #251)."""

import pytest
from datetime import datetime, UTC, timedelta
from zoneinfo import ZoneInfo
import numpy as np

from app.config import RiskConfig
from app.core.canonical_evidence import CanonicalEvidence, CanonicalOutcomeResolver
from app.core.cohort_fingerprint import (
    generate_cohort_fingerprint,
    generate_strategy_manifest_hash,
    validate_cohort_fingerprint,
)
from app.core.promotion import (
    PolicyPromotionGovernor,
    PromotionEvidenceEnvelope,
    SafetyCheckEvidence,
    SafetyCheckStatus,
)
from app.core.shadow_replay_eligibility import (
    ReplayEligibilityState,
    classify_shadow_replay_status,
    get_replay_eligibility_sql_filter,
)
from app.db.database import Database, ShadowLedgerRow
from app.strategies.memory import _canonical_realized_r

ET = ZoneInfo("America/New_York")


def test_ticket_224_canonical_evidence_resolution():
    """Test #224: Authoritative Canonical Evidence outcome resolution."""
    valid_rec = {
        "replayed": True,
        "replay_failed": False,
        "simulated_pnl": 150.0,
        "entry_price": 100.0,
        "stop_loss": 97.0,
        "regime": "TREND",
        "regime_source": "VIX",
    }
    ev = CanonicalOutcomeResolver.resolve(valid_rec)
    assert ev.valid is True
    assert ev.realized_r is not None
    assert ev.realized_r > 0
    assert ev.invalid_reason is None


def test_ticket_234_breakeven_vs_missing_r():
    """Test #234: realized_r = 0.0 is valid 0R breakeven trade, whereas None is missing R evidence."""
    r_breakeven = CanonicalOutcomeResolver.compute_canonical_r(pnl=0.0, entry=100.0, stop=98.0)
    assert r_breakeven == 0.0

    r_missing = CanonicalOutcomeResolver.compute_canonical_r(pnl=None, entry=100.0, stop=98.0)
    assert r_missing is None


def test_ticket_238_memory_canonical_r_returns_none_for_missing():
    """Test #238: _canonical_realized_r in memory.py returns None for missing R evidence, preserving 0.0 breakeven."""
    outcomes_bk = {"15m": {"realized_r": 0.0}}
    assert _canonical_realized_r("rsi2", outcomes_bk) == 0.0

    outcomes_missing = {"15m": {"realized_r": None}}
    assert _canonical_realized_r("rsi2", outcomes_missing) is None


def test_ticket_244_shared_replay_eligibility_predicate_and_telemetry(tmp_path):
    """Test #244 Hardening #1: Shared replay eligibility predicate distinguishing all 5 states."""
    db_file = tmp_path / "test_244_eligibility.db"
    db = Database(db_file)

    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    # 1. Pending but not yet eligible (signal 10m ago, min delay 60m)
    ts_recent = now - timedelta(minutes=10)
    db.save_shadow_signal("cand_recent", ts_recent, "AAPL", "orb", "long", "demo", {"candidate_id": "cand_recent", "final_decision": "rejected", "replayed": False})

    # 2. Eligible (signal 2h ago)
    ts_eligible = now - timedelta(hours=2)
    db.save_shadow_signal("cand_eligible", ts_eligible, "AAPL", "orb", "long", "demo", {"candidate_id": "cand_eligible", "final_decision": "rejected", "replayed": False})

    # 3. Expired / unreplayed (signal 10 days ago, max age 7 days)
    ts_expired = now - timedelta(days=10)
    db.save_shadow_signal("cand_expired", ts_expired, "AAPL", "orb", "long", "demo", {"candidate_id": "cand_expired", "final_decision": "rejected", "replayed": False})

    # 4. Failed replay
    ts_failed = now - timedelta(hours=3)
    db.save_shadow_signal("cand_failed", ts_failed, "AAPL", "orb", "long", "demo", {"candidate_id": "cand_failed", "final_decision": "rejected", "replayed": True, "replay_failed": True})

    # 5. Successfully replayed
    ts_success = now - timedelta(hours=4)
    db.save_shadow_signal("cand_success", ts_success, "AAPL", "orb", "long", "demo", {"candidate_id": "cand_success", "final_decision": "rejected", "replayed": True, "replay_failed": False, "replay_timestamp": (now - timedelta(hours=1)).isoformat()})

    # Query telemetry with min_delay_seconds = 3600 (60m) and max_age_seconds = 86400 * 7 (7d)
    telem = db.get_replay_queue_telemetry(mode="demo", now=now, min_delay_seconds=3600.0, max_age_seconds=86400.0 * 7.0)

    assert telem["replay_pending_total"] == 2
    assert telem["replay_pending_eligible"] == 1
    assert telem["replay_pending_not_eligible"] == 1
    assert telem["replay_expired_unreplayed"] == 1
    assert telem["replay_failed"] == 1
    assert telem["replay_successfully_replayed"] == 1

    # Scheduler fetches only eligible records when min_delay_seconds = 3600.0
    pending_eligible = db.get_pending_shadow_replays(mode="demo", now=now, limit=10, min_delay_seconds=3600.0)
    assert len(pending_eligible) == 1
    assert pending_eligible[0]["candidate_id"] == "cand_eligible"

    # Test Python in-memory classifier on all 5 states
    rec_not_elig = {"final_decision": "rejected", "replayed": False, "ts": ts_recent}
    assert classify_shadow_replay_status(rec_not_elig, now=now, min_delay_seconds=3600.0) == ReplayEligibilityState.PENDING_NOT_ELIGIBLE

    rec_elig = {"final_decision": "rejected", "replayed": False, "ts": ts_eligible}
    assert classify_shadow_replay_status(rec_elig, now=now, min_delay_seconds=3600.0) == ReplayEligibilityState.ELIGIBLE

    rec_exp = {"final_decision": "rejected", "replayed": False, "ts": ts_expired}
    assert classify_shadow_replay_status(rec_exp, now=now, min_delay_seconds=3600.0) == ReplayEligibilityState.EXPIRED_UNREPLAYED

    rec_fl = {"final_decision": "rejected", "replayed": True, "replay_failed": True, "ts": ts_failed}
    assert classify_shadow_replay_status(rec_fl, now=now) == ReplayEligibilityState.FAILED

    rec_succ = {"final_decision": "rejected", "replayed": True, "replay_failed": False, "ts": ts_success}
    assert classify_shadow_replay_status(rec_succ, now=now) == ReplayEligibilityState.SUCCESSFULLY_REPLAYED


def test_ticket_237_composite_observation_identity_matching():
    """Test #237 Hardening #2: Matching by composite observation identity and matched denominator N."""
    ts_iso = "2026-07-22T10:00:00+00:00"

    champ_recs = [
        {
            "candidate_id": f"cand_{i}",
            "policy_id": "policy_champion",
            "timestamp": ts_iso,
            "strategy": "orb",
            "direction": "long",
            "final_decision": "rejected",
            "replayed": True,
            "simulated_pnl": 100.0,
            "entry_price": 100.0,
            "stop_loss": 98.0,
        }
        for i in range(35)
    ]

    cand_recs = [
        {
            "candidate_id": f"cand_{i}",
            "policy_id": "policy_challenger",
            "timestamp": ts_iso,
            "strategy": "orb",
            "direction": "long",
            "final_decision": "rejected",
            "replayed": True,
            "simulated_pnl": 150.0,
            "entry_price": 100.0,
            "stop_loss": 98.0,
        }
        for i in range(35)
    ]

    # Matching on exact composite identity produces matched_valid_count = 35
    res = CanonicalOutcomeResolver.calculate_matched_nomination_evidence(champ_recs, cand_recs, min_promotion_sample=30)
    assert res["matched_valid_count"] == 35
    assert res["nomination_eligible"] is True
    assert res["expectancy_gain_r"] > 0

    # Insufficient matched count (10 < 30) fails nomination
    res_small = CanonicalOutcomeResolver.calculate_matched_nomination_evidence(champ_recs[:10], cand_recs[:10], min_promotion_sample=30)
    assert res_small["matched_valid_count"] == 10
    assert res_small["nomination_eligible"] is False
    assert res_small["reason"] == "INSUFFICIENT_MATCHED_SAMPLES"


def test_ticket_253_cohort_fingerprint_generation():
    """Test #253: Cohort fingerprint generation is deterministic and SHA-256 bound."""
    manifest_a = [{"policy_id": "p1", "param": 1}, {"policy_id": "p2", "param": 2}]
    manifest_b = [{"policy_id": "p1", "param": 1}, {"policy_id": "p2", "param": 2}]

    fp_a = generate_cohort_fingerprint(manifest_a)
    fp_b = generate_cohort_fingerprint(manifest_b)

    assert fp_a == fp_b
    assert len(fp_a) == 64
    assert validate_cohort_fingerprint(manifest_a, fp_a) is True


def test_ticket_254_strategy_manifest_hashing():
    """Test #254: Strategy manifest hashing and fingerprint validation."""
    strat_manifest = {"strategy_name": "rsi2_reversion", "timeframe": "1m", "params": {"period": 2, "oversold": 10}}
    h1 = generate_strategy_manifest_hash(strat_manifest)
    h2 = generate_strategy_manifest_hash(strat_manifest)
    assert h1 == h2
    assert len(h1) == 64


def test_ticket_248_freshness_validation_in_governor():
    """Test #248 Hardening #3: Safety check evidence freshness validation."""
    db_mock = None
    gov = PolicyPromotionGovernor(db=db_mock)

    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    # 1. Fresh PASS (executed 1h ago) -> eligible
    fresh_check = SafetyCheckEvidence(
        rule_name="min_observations",
        status=SafetyCheckStatus.PASS,
        details="Fresh PASS",
        timestamp=(now - timedelta(hours=1)).isoformat()
    )
    all_passed, evaluated = gov.evaluate_safety_checks([fresh_check], now=now, max_evidence_age_seconds=86400.0)
    assert all_passed is True
    assert evaluated[0].status == SafetyCheckStatus.PASS

    # 2. Too old PASS (executed 48h ago, max age 24h) -> STALE -> reject
    old_check = SafetyCheckEvidence(
        rule_name="min_observations",
        status=SafetyCheckStatus.PASS,
        details="Old PASS",
        timestamp=(now - timedelta(hours=48)).isoformat()
    )
    all_passed_old, evaluated_old = gov.evaluate_safety_checks([old_check], now=now, max_evidence_age_seconds=86400.0)
    assert all_passed_old is False
    assert evaluated_old[0].status == SafetyCheckStatus.STALE

    # 3. FAIL / ERROR / NOT_RUN -> reject
    fail_check = SafetyCheckEvidence(rule_name="stress_test", status=SafetyCheckStatus.FAIL, details="Failed")
    all_passed_fail, _ = gov.evaluate_safety_checks([fail_check], now=now)
    assert all_passed_fail is False


def test_ticket_249_configurable_promotion_criteria():
    """Test #249: Promotion criteria parameters are configurable in RiskConfig."""
    cfg = RiskConfig(
        promotion_min_valid_observations=60,
        promotion_min_win_rate_pct=58.0,
        promotion_min_expectancy_r=0.25,
    )
    assert cfg.promotion_min_valid_observations == 60
    assert cfg.promotion_min_win_rate_pct == 58.0
    assert cfg.promotion_min_expectancy_r == 0.25


def test_ticket_251_envelope_cryptographic_binding_and_tamper_rejection(tmp_path):
    """Test #251 Hardening #4: Binding #249 results & thresholds into signed envelope."""
    db_file = tmp_path / "test_251_governance.db"
    db = Database(db_file)
    gov = PolicyPromotionGovernor(db=db)

    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    sc = SafetyCheckEvidence(
        rule_name="min_observations",
        status=SafetyCheckStatus.PASS,
        details="35 valid observations",
        timestamp=now.isoformat()
    )

    eval_results = {
        "matched_valid_count": 35,
        "win_rate": 62.5,
        "expectancy_r": 0.35,
        "oos_window_count": 4,
        "walk_forward_stability_result": "STABLE"
    }

    thresholds_used = {
        "promotion_min_valid_observations": 30,
        "promotion_min_win_rate_pct": 50.0,
        "promotion_min_expectancy_r": 0.1
    }

    envelope = PromotionEvidenceEnvelope(
        policy_id="policy_challenger_v2",
        git_commit_sha="a1b2c3d",
        package_hash="hash_pack_789",
        safety_checks=[sc],
        canonical_evidence_metadata={"dataset_window_id": "win_202607"},
        criteria_evaluation_results=eval_results,
        configured_thresholds_used=thresholds_used,
        criteria_evaluation_version="v1.0",
        timestamp=now.isoformat(),
    )

    assert envelope.verify_integrity() is True

    snapshot = {"ml_threshold": 0.60, "policy_id": "policy_challenger_v2"}
    res = gov.execute_atomic_promotion(envelope, snapshot, now=now)
    assert res["status"] == "PROMOTED"

    # Tamper test: modifying criteria evaluation result invalidates envelope signature
    envelope.criteria_evaluation_results["expectancy_r"] = 0.99
    assert envelope.verify_integrity() is False

    with pytest.raises(ValueError, match="signature invalid"):
        gov.execute_atomic_promotion(envelope, snapshot, now=now)
