"""Authoritative Replay Eligibility Contract and Predicate Helper (#244).

Provides a single source of truth for shadow signal replay eligibility,
shared between the replay scheduler, telemetry calculations, and Python evaluation.
"""

from __future__ import annotations

from enum import StrEnum
from datetime import datetime, UTC
from zoneinfo import ZoneInfo
from typing import Any
from sqlalchemy import or_, and_, ColumnElement

ET_ZONE = ZoneInfo("America/New_York")


class ReplayEligibilityState(StrEnum):
    """Authoritative states for shadow signal replay eligibility (#244)."""
    SUCCESSFULLY_REPLAYED = "successfully_replayed"
    FAILED = "failed"
    EXPIRED_UNREPLAYED = "expired_unreplayed"
    ELIGIBLE = "eligible"
    PENDING_NOT_ELIGIBLE = "pending_not_eligible"


import re

_CANDIDATE_ID_TS_REGEX = re.compile(r"(\d{8})_(\d{6})")


def parse_datetime_to_utc(dt_val: Any, candidate_id: str | None = None) -> datetime | None:
    """Helper to parse datetime values into aware UTC datetime.
    
    If candidate_id is provided, cross-validates against candidate_id timestamp
    pattern (YYYYMMDD_HHMMSS) to prevent timezone double-conversion +4h shifts (#244).
    """
    candidate_dt = None
    if candidate_id and isinstance(candidate_id, str):
        m = _CANDIDATE_ID_TS_REGEX.search(candidate_id)
        if m:
            date_str, time_str = m.groups()
            try:
                candidate_dt = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
            except Exception:
                pass

    dt = None
    if dt_val is not None:
        if isinstance(dt_val, str):
            try:
                dt = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
            except Exception:
                dt = None
        elif isinstance(dt_val, datetime):
            dt = dt_val

    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)

    # If candidate_dt exists, check if stored dt has a positive +4h or +5h shift relative to candidate_dt
    if candidate_dt and dt:
        signed_diff = (dt - candidate_dt).total_seconds()
        # Stored timestamp dt was shifted ahead of candidate_dt by +4h (14400s, EDT) or +5h (18000s, EST)
        # due to legacy double ET timezone conversion. Tight ±60s tolerance window.
        if abs(signed_diff - 14400) <= 60 or abs(signed_diff - 18000) <= 60:
            return candidate_dt

    return dt or candidate_dt


def classify_shadow_replay_status(
    record: dict[str, Any],
    now: datetime | None = None,
    min_delay_seconds: float = 3600.0,
    max_age_seconds: float = 86400.0 * 7.0,
) -> ReplayEligibilityState:
    """Classify in-memory shadow record into authoritative ReplayEligibilityState (#244)."""
    if now is None:
        now = datetime.now(UTC)
    else:
        now = parse_datetime_to_utc(now) or datetime.now(UTC)

    replayed = bool(record.get("replayed", False))
    replay_failed = bool(record.get("replay_failed", False))

    if replayed and not replay_failed:
        return ReplayEligibilityState.SUCCESSFULLY_REPLAYED
    if replay_failed:
        return ReplayEligibilityState.FAILED

    final_decision = str(record.get("final_decision", "")).lower()
    if final_decision != "rejected":
        # Non-rejected signals are not shadow replay candidates
        return ReplayEligibilityState.PENDING_NOT_ELIGIBLE

    ts_val = record.get("ts") or record.get("timestamp")
    dt_ts = parse_datetime_to_utc(ts_val)

    if dt_ts is None:
        return ReplayEligibilityState.PENDING_NOT_ELIGIBLE

    age_seconds = (now - dt_ts).total_seconds()

    if age_seconds > max_age_seconds:
        return ReplayEligibilityState.EXPIRED_UNREPLAYED
    if age_seconds < min_delay_seconds:
        return ReplayEligibilityState.PENDING_NOT_ELIGIBLE

    return ReplayEligibilityState.ELIGIBLE


def get_replay_eligibility_sql_filter(
    table_class: Any,
    now: datetime | None = None,
    min_delay_seconds: float = 3600.0,
    max_age_seconds: float = 86400.0 * 7.0,
    mode: str = "demo",
    state: ReplayEligibilityState = ReplayEligibilityState.ELIGIBLE,
) -> ColumnElement[bool]:
    """Generate authoritative SQL filter expression for a given ReplayEligibilityState (#244)."""
    if now is None:
        now = datetime.now(UTC)

    # Base un-replayed, non-failed rejected condition
    rejected_cond = table_class.final_decision == "rejected"
    mode_cond = table_class.mode == mode
    not_replayed_cond = or_(table_class.replayed == None, table_class.replayed == False)  # noqa: E711
    not_failed_cond = or_(table_class.replay_failed == None, table_class.replay_failed == False)  # noqa: E711

    if state == ReplayEligibilityState.SUCCESSFULLY_REPLAYED:
        return and_(mode_cond, table_class.replayed == True, not_failed_cond)  # noqa: E712

    if state == ReplayEligibilityState.FAILED:
        return and_(mode_cond, table_class.replay_failed == True)  # noqa: E712

    base_pending = and_(mode_cond, rejected_cond, not_replayed_cond, not_failed_cond)

    # Cutoff timestamps: ts <= min_delay_cutoff for min delay; ts >= max_age_cutoff for max age
    from datetime import timedelta
    min_delay_cutoff = now - timedelta(seconds=min_delay_seconds)
    max_age_cutoff = now - timedelta(seconds=max_age_seconds)

    if state == ReplayEligibilityState.ELIGIBLE:
        return and_(base_pending, table_class.ts <= min_delay_cutoff, table_class.ts >= max_age_cutoff)

    if state == ReplayEligibilityState.PENDING_NOT_ELIGIBLE:
        return and_(base_pending, table_class.ts > min_delay_cutoff, table_class.ts >= max_age_cutoff)

    if state == ReplayEligibilityState.EXPIRED_UNREPLAYED:
        return and_(base_pending, table_class.ts < max_age_cutoff)

    return base_pending
