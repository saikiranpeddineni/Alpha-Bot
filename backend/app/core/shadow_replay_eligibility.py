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


def parse_datetime_to_utc(dt_val: Any) -> datetime | None:
    """Helper to parse datetime values into aware UTC datetime."""
    if dt_val is None:
        return None
    if isinstance(dt_val, str):
        try:
            dt = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
        except Exception:
            return None
    elif isinstance(dt_val, datetime):
        dt = dt_val
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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
