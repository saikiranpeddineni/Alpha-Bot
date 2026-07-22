"""Deterministic Cohort Fingerprint Generator (#253).

Derives SHA-256 fingerprint from a canonical, sorted representation of candidate policy cohort parameters.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def generate_cohort_fingerprint(cohort_manifest: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Generate deterministic SHA-256 fingerprint for policy cohort manifest (#253)."""
    canonical_json = json.dumps(cohort_manifest, sort_keys=True, default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def generate_strategy_manifest_hash(strategy_manifest: dict[str, Any] | list[dict[str, Any]]) -> str:
    """Generate deterministic SHA-256 hash for strategy manifest items (#254)."""
    canonical_json = json.dumps(strategy_manifest, sort_keys=True, default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def validate_cohort_fingerprint(manifest: Any, expected_fingerprint: str) -> bool:
    """Validate that cohort manifest produces expected SHA-256 fingerprint (#253, #254)."""
    computed = generate_cohort_fingerprint(manifest)
    return computed == expected_fingerprint

