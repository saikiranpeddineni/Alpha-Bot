"""Child Policy Configuration and Immutability Module.

Defines the structure for child policy configurations, canonical JSON serialization,
and SHA256 fingerprint calculation to guarantee immutable research identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


@dataclass
class ChildPolicyConfig:
    policy_id: str
    parent_policy_id: str = "champion"
    policy_version: str = "v1"
    spawned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seed_capital: float = 10000.0
    status: str = "ACTIVE"
    execution_mode: str = "SHADOW_ONLY"
    
    # Strategy overrides (e.g., {"rsi2_period": 3, "trailing_stop_atr_mult": 1.5})
    strategy_overrides: dict[str, Any] = field(default_factory=dict)
    # ML thresholds/config overrides (e.g., {"ml_threshold": 0.45})
    ml_overrides: dict[str, Any] = field(default_factory=dict)
    # Meta labeler overrides (e.g., {"meta_threshold": 0.50})
    meta_overrides: dict[str, Any] = field(default_factory=dict)
    # Risk & portfolio sizing overrides
    risk_overrides: dict[str, Any] = field(default_factory=dict)
    
    code_commit_sha: str | None = None
    model_manifest_sha: str | None = None
    spawn_reason: str | None = None
    created_from_candidate: str | None = None

    def canonical_json(self) -> str:
        """Returns deterministic, sorted canonical JSON representation of configuration."""
        d = {
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
        return json.dumps(d, sort_keys=True, separators=(',', ':'))

    def compute_fingerprint(self) -> str:
        """Computes SHA256 hex digest of the canonical configuration snapshot."""
        return hashlib.sha256(self.canonical_json().encode('utf-8')).hexdigest()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChildPolicyConfig:
        spawned_at_raw = d.get("spawned_at")
        if isinstance(spawned_at_raw, str):
            try:
                sp_at = datetime.fromisoformat(spawned_at_raw)
            except ValueError:
                sp_at = datetime.now(timezone.utc)
        elif isinstance(spawned_at_raw, datetime):
            sp_at = spawned_at_raw
        else:
            sp_at = datetime.now(timezone.utc)

        return cls(
            policy_id=d["policy_id"],
            parent_policy_id=d.get("parent_policy_id", "champion"),
            policy_version=d.get("policy_version", "v1"),
            spawned_at=sp_at,
            seed_capital=float(d.get("seed_capital", 10000.0)),
            status=d.get("status", "ACTIVE"),
            execution_mode=d.get("execution_mode", "SHADOW_ONLY"),
            strategy_overrides=d.get("strategy_overrides") or {},
            ml_overrides=d.get("ml_overrides") or {},
            meta_overrides=d.get("meta_overrides") or {},
            risk_overrides=d.get("risk_overrides") or {},
            code_commit_sha=d.get("code_commit_sha"),
            model_manifest_sha=d.get("model_manifest_sha"),
            spawn_reason=d.get("spawn_reason"),
            created_from_candidate=d.get("created_from_candidate"),
        )
