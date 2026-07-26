"""Stable candidate/event identity helpers for the production-to-shadow boundary."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def _utc_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("candidate timestamp must be timezone-aware")
    return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")


def canonical_candidate_id(*, symbol: str, strategy: str, side: str, decision_ts: datetime, ordinal: int = 0) -> str:
    """Return deterministic ID for one Champion candidate.

    ``ordinal`` disambiguates multiple same-strategy/same-side candidates produced on
    the same decision timestamp without introducing randomness.
    """
    raw = "|".join((symbol.upper(), strategy.lower(), side.lower(), _utc_iso(decision_ts), str(int(ordinal))))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20].upper()
    return f"CAND-{digest}"


def canonical_event_id(*, candidate_id: str | None, event_type: str, ts: datetime, symbol: str) -> str:
    """Return deterministic idempotency key for a canonical observation event."""
    raw = "|".join((candidate_id or "NONE", event_type.upper(), symbol.upper(), _utc_iso(ts)))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24].upper()
    return f"EVT-{digest}"
