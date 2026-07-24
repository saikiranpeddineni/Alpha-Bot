"""Immutable child-policy configuration for the shadow spawn laboratory."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any


@dataclass
class ChildPolicyConfig:
    policy_id: str
    parent_policy_id: str = "champion"
    policy_version: str = "v1"
    spawned_at: datetime | None = field(default_factory=lambda: datetime.now(timezone.utc))
    seed_capital: float = 10000.0
    status: str = "ACTIVE"
    execution_mode: str = "SHADOW_ONLY"
    unavailable_reason: str | None = None
    strategy_overrides: dict[str, Any] = field(default_factory=dict)
    ml_overrides: dict[str, Any] = field(default_factory=dict)
    meta_overrides: dict[str, Any] = field(default_factory=dict)
    risk_overrides: dict[str, Any] = field(default_factory=dict)
    code_commit_sha: str | None = None
    model_manifest_sha: str | None = None
    spawn_reason: str | None = None
    created_from_candidate: str | None = None

    def __post_init__(self) -> None:
        if self.execution_mode != "SHADOW_ONLY":
            raise ValueError("Spawn laboratory policies must remain SHADOW_ONLY")
        if not self.policy_id:
            raise ValueError("policy_id is required")
        if not math.isfinite(float(self.seed_capital)) or self.seed_capital <= 0:
            raise ValueError("seed_capital must be positive and finite")

    def identity_dict(self) -> dict[str, Any]:
        """Immutable policy behavior identity; lifecycle timestamps are provenance, not behavior."""
        return {
            "policy_id": self.policy_id,
            "parent_policy_id": self.parent_policy_id,
            "policy_version": self.policy_version,
            "seed_capital": self.seed_capital,
            "execution_mode": self.execution_mode,
            "strategy_overrides": self.strategy_overrides,
            "ml_overrides": self.ml_overrides,
            "meta_overrides": self.meta_overrides,
            "risk_overrides": self.risk_overrides,
            "code_commit_sha": self.code_commit_sha,
            "model_manifest_sha": self.model_manifest_sha,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.identity_dict(), sort_keys=True, separators=(",", ":"))

    def snapshot_json(self) -> str:
        """Durable restart snapshot. Fingerprint still covers immutable behavior identity."""
        d = dict(self.identity_dict())
        d.update({
            "spawned_at": self.spawned_at.isoformat() if isinstance(self.spawned_at, datetime) else None,
            "status": self.status,
            "spawn_reason": self.spawn_reason,
            "created_from_candidate": self.created_from_candidate,
        })
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def compute_fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChildPolicyConfig":
        spawned_at_raw = d.get("spawned_at")
        status = d.get("status", "ACTIVE")
        reason = None
        sp_at = None
        if isinstance(spawned_at_raw, str):
            try:
                sp_at = datetime.fromisoformat(spawned_at_raw)
                if sp_at.tzinfo is None:
                    status, reason = "UNAVAILABLE", "NAIVE_SPAWN_TIMESTAMP"
            except ValueError:
                status, reason = "UNAVAILABLE", "INVALID_SPAWN_TIMESTAMP_FORMAT"
        elif isinstance(spawned_at_raw, datetime):
            sp_at = spawned_at_raw
            if sp_at.tzinfo is None:
                status, reason = "UNAVAILABLE", "NAIVE_SPAWN_TIMESTAMP"
        else:
            status, reason = "UNAVAILABLE", "MISSING_SPAWN_TIMESTAMP"
        return cls(
            policy_id=d["policy_id"], parent_policy_id=d.get("parent_policy_id", "champion"),
            policy_version=d.get("policy_version", "v1"), spawned_at=sp_at,
            seed_capital=float(d.get("seed_capital", 10000.0)), status=status,
            execution_mode=d.get("execution_mode", "SHADOW_ONLY"), unavailable_reason=reason,
            strategy_overrides=d.get("strategy_overrides") or {}, ml_overrides=d.get("ml_overrides") or {},
            meta_overrides=d.get("meta_overrides") or {}, risk_overrides=d.get("risk_overrides") or {},
            code_commit_sha=d.get("code_commit_sha"), model_manifest_sha=d.get("model_manifest_sha"),
            spawn_reason=d.get("spawn_reason"), created_from_candidate=d.get("created_from_candidate"),
        )
