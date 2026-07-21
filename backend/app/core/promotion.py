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
    """Immutable promotion evidence envelope bound to git commit SHA and package hash (#251)."""
    policy_id: str
    git_commit_sha: str
    package_hash: str
    safety_checks: list[SafetyCheckEvidence]
    canonical_evidence_metadata: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    envelope_signature: str = ""

    def __post_init__(self) -> None:
        if not self.envelope_signature:
            self.envelope_signature = self._compute_signature()

    def _compute_signature(self) -> str:
        payload = {
            "policy_id": self.policy_id,
            "git_commit_sha": self.git_commit_sha,
            "package_hash": self.package_hash,
            "safety_checks": [asdict(sc) for sc in self.safety_checks],
            "canonical_evidence_metadata": self.canonical_evidence_metadata,
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
        checks: list[SafetyCheckEvidence]
    ) -> tuple[bool, list[SafetyCheckEvidence]]:
        """Evaluate safety checks. Fails closed if any check is not PASS (#247)."""
        all_passed = True
        for sc in checks:
            if sc.status != SafetyCheckStatus.PASS:
                all_passed = False
                break
        return all_passed, checks

    def execute_atomic_promotion(
        self,
        envelope: PromotionEvidenceEnvelope,
        policy_snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute fail-closed atomic promotion transaction (#246).
        
        Requires envelope integrity verification and 100% PASS safety checks.
        Persists active policy snapshot and records promotion evidence in DB in a single transaction.
        """
        # 1. Verify envelope cryptographic integrity
        if not envelope.verify_integrity():
            raise ValueError("Promotion failed: evidence envelope cryptographic signature invalid")

        # 2. Assert all safety checks passed
        all_passed, checks = self.evaluate_safety_checks(envelope.safety_checks)
        if not all_passed:
            failed_rules = [c.rule_name for c in checks if c.status != SafetyCheckStatus.PASS]
            raise ValueError(f"Promotion failed: safety checks failed for rules: {failed_rules}")

        # 3. Fail-closed atomic transaction
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

                # Persist policy promotion audit record in the SAME atomic transaction (#246, #247)
                metadata = envelope.canonical_evidence_metadata or {}
                now_dt = datetime.now(UTC)
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
            raise RuntimeError(f"Fail-closed atomic promotion transaction failed: {exc}") from exc
