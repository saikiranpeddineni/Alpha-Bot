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
