from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class EvidenceFreshness:
    age_seconds: float
    fresh: bool
    reason: str


def assess_evidence_freshness(
    *,
    evidence_time: datetime,
    now: datetime | None = None,
    max_age_seconds: float = 150.0,
) -> EvidenceFreshness:
    now = now or datetime.now(timezone.utc)
    if evidence_time.tzinfo is None:
        evidence_time = evidence_time.replace(tzinfo=timezone.utc)
    age = max(0.0, (now - evidence_time).total_seconds())
    fresh = age <= max_age_seconds
    return EvidenceFreshness(
        age_seconds=round(age, 3),
        fresh=fresh,
        reason="PIS_EVIDENCE_FRESH" if fresh else "PIS_EVIDENCE_STALE",
    )
