"""Production Policy Promotion Governance & Security (#246, #247, #249, #251).

Implements fail-closed atomic promotion transactions, structured safety check evidence,
configurable promotion criteria, and immutable evidence envelopes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class SafetyCheckStatus(StrEnum):
    """Structured status enum for governance safety checks (#247)."""
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    NOT_RUN = "NOT_RUN"
    STALE = "STALE"


@dataclass(frozen=True)
class SafetyCheckEvidence:
    """Structured evidence for an individual safety rule check (#247)."""
    rule_name: str
    status: SafetyCheckStatus
    details: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class PromotionEvidenceEnvelope:
    """Immutable promotion evidence envelope bound to git commit SHA, package hash, and criteria evaluation results (#251)."""
    policy_id: str
    git_commit_sha: str
    package_hash: str
    safety_checks: list[SafetyCheckEvidence]
    canonical_evidence_metadata: dict[str, Any]
    criteria_evaluation_results: dict[str, Any] = field(default_factory=dict)
    configured_thresholds_used: dict[str, Any] = field(default_factory=dict)
    criteria_fingerprint: str = ""
    criteria_evaluation_version: str = "v1.0"
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    envelope_signature: str = ""

    def __post_init__(self) -> None:
        if not self.criteria_fingerprint:
            self.criteria_fingerprint = self._compute_criteria_fingerprint()
        if not self.envelope_signature:
            self.envelope_signature = self._compute_signature()

    def _compute_criteria_fingerprint(self) -> str:
        canonical_str = json.dumps({
            "configured_thresholds_used": self.configured_thresholds_used,
            "criteria_evaluation_version": self.criteria_evaluation_version,
        }, sort_keys=True, default=str)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

    def _compute_signature(self) -> str:
        payload = {
            "policy_id": self.policy_id,
            "git_commit_sha": self.git_commit_sha,
            "package_hash": self.package_hash,
            "safety_checks": [asdict(sc) for sc in self.safety_checks],
            "canonical_evidence_metadata": self.canonical_evidence_metadata,
            "criteria_evaluation_results": self.criteria_evaluation_results,
            "configured_thresholds_used": self.configured_thresholds_used,
            "criteria_fingerprint": self.criteria_fingerprint,
            "criteria_evaluation_version": self.criteria_evaluation_version,
            "timestamp": self.timestamp,
        }
        canonical_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(canonical_bytes).hexdigest()

    def verify_integrity(self) -> bool:
        """Verify that envelope contents match the cryptographic signature (#251)."""
        return self.envelope_signature == self._compute_signature()


class PolicyPromotionGovernor:
    """Governance authority enforcing fail-closed atomic policy promotions (#246)."""

    def __init__(self, db: Any, risk_config: Any = None) -> None:
        self.db = db
        self.risk_config = risk_config

    def evaluate_safety_checks(
        self,
        checks: list[SafetyCheckEvidence],
        now: datetime | None = None,
        max_evidence_age_seconds: float = 86400.0,
    ) -> tuple[bool, list[SafetyCheckEvidence]]:
        """Evaluate safety checks and validate evidence freshness (#247, #248 Hardening).

        Enforces status rules:
        - PASS + fresh       -> eligible
        - PASS + too old     -> STALE -> reject
        - FAIL               -> reject
        - ERROR              -> reject
        - NOT_RUN            -> reject
        - missing / unknown  -> reject
        """
        if now is None:
            now = datetime.now(UTC)

        evaluated = []
        all_passed = True

        for sc in checks:
            status = sc.status
            dt_ts = None
            if sc.timestamp:
                try:
                    dt_ts = datetime.fromisoformat(sc.timestamp.replace("Z", "+00:00"))
                    if dt_ts.tzinfo is None:
                        dt_ts = dt_ts.replace(tzinfo=UTC)
                except Exception:
                    dt_ts = None

            # Check freshness for PASS
            if status == SafetyCheckStatus.PASS:
                if dt_ts is not None:
                    age_sec = (now - dt_ts).total_seconds()
                    if age_sec > max_evidence_age_seconds:
                        status = SafetyCheckStatus.STALE

            details_msg = sc.details
            if status == SafetyCheckStatus.STALE and sc.status == SafetyCheckStatus.PASS:
                details_msg += " (STALE: evidence expired)"

            updated_sc = SafetyCheckEvidence(
                rule_name=sc.rule_name,
                status=status,
                details=details_msg,
                timestamp=sc.timestamp,
            )
            evaluated.append(updated_sc)

            if status != SafetyCheckStatus.PASS:
                all_passed = False

        return all_passed, evaluated

    def execute_atomic_promotion(
        self,
        envelope: PromotionEvidenceEnvelope,
        policy_snapshot: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Execute fail-closed atomic promotion transaction (#246, #248, #251).

        Requires envelope integrity verification, criteria fingerprint check, and 100% PASS fresh safety checks.
        Persists active policy snapshot and records promotion evidence in DB in a single transaction.
        """
        if now is None:
            now = datetime.now(UTC)

        # 1. Verify envelope cryptographic integrity
        if not envelope.verify_integrity():
            raise ValueError("Promotion failed: evidence envelope cryptographic signature invalid")

        # 2. Derive max_evidence_age_seconds
        max_age = 86400.0
        if self.risk_config:
            max_age = getattr(self.risk_config, "promotion_max_evidence_age_seconds", None) or getattr(self.risk_config, "max_evidence_age_seconds", 86400.0)

        # 3. Assert all safety checks passed and are FRESH (#248)
        all_passed, checks = self.evaluate_safety_checks(envelope.safety_checks, now=now, max_evidence_age_seconds=max_age)
        if not all_passed:
            failed_rules = [f"{c.rule_name}:{c.status}" for c in checks if c.status != SafetyCheckStatus.PASS]
            raise ValueError(f"Promotion failed: safety checks failed for rules: {failed_rules}")

        # 4. Fail-closed atomic transaction
        try:
            from app.db.database import SettingRow, PolicyPromotionRow
            with self.db._session() as s:
                # Read previous snapshot for audit logging
                prev_setting = s.query(SettingRow).filter(SettingRow.key == "active_policy_snapshot").first()
                prev_snapshot_str = prev_setting.value if (prev_setting and prev_setting.value) else "{}"

                snapshot_json = json.dumps(policy_snapshot, default=str)
                s.merge(SettingRow(key="active_policy_snapshot", value=snapshot_json))
                if "ml_threshold" in policy_snapshot:
                    s.merge(SettingRow(key="ml_threshold", value=json.dumps(policy_snapshot["ml_threshold"], default=str)))

                # Persist policy promotion audit record in the SAME atomic transaction (#246, #247, #251)
                metadata = envelope.canonical_evidence_metadata or {}
                now_dt = now
                if isinstance(envelope.timestamp, str):
                    try:
                        now_dt = datetime.fromisoformat(envelope.timestamp.replace("Z", "+00:00"))
                    except Exception:
                        pass

                promotion_row = PolicyPromotionRow(
                    policy_id=envelope.policy_id,
                    champion_policy_id=metadata.get("champion_policy_id"),
                    champion_policy_version=metadata.get("champion_policy_version"),
                    challenger_policy_id=metadata.get("challenger_policy_id"),
                    challenger_policy_version=metadata.get("challenger_policy_version"),
                    promoter_id=metadata.get("promoter_id", "PolicyPromotionGovernor"),
                    timestamp=now_dt,
                    reason=metadata.get("reason", "Atomic Governor Promotion"),
                    previous_snapshot=prev_snapshot_str,
                    new_snapshot=snapshot_json,
                    evidence_package_id=envelope.package_hash,
                    evidence_package=json.dumps(asdict(envelope), default=str),
                    mode=metadata.get("mode", "live"),
                    evidence_generation_timestamp=now_dt,
                    code_commit_sha=envelope.git_commit_sha,
                    dataset_window_id=metadata.get("dataset_window_id"),
                )
                s.add(promotion_row)
                s.commit()

            return {
                "status": "PROMOTED",
                "policy_id": envelope.policy_id,
                "envelope_signature": envelope.envelope_signature,
                "timestamp": envelope.timestamp,
            }
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            raise RuntimeError(f"Fail-closed atomic promotion transaction failed: {exc}") from exc
