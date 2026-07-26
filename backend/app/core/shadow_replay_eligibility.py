"""Authoritative Replay Eligibility Contract and Predicate Helper (#244).

Provides a single source of truth for shadow signal replay eligibility,
shared between the replay scheduler, telemetry calculations, and Python evaluation.
"""

from __future__ import annotations

from enum import StrEnum
from datetime import datetime, UTC, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from sqlalchemy import or_, and_, ColumnElement

from app.core.time_utils import ensure_utc, parse_utc, utc_now

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
    """Parse persisted datetime values into timezone-aware UTC.

    Candidate IDs encode the historical UTC signal timestamp.  They are used only to
    repair the known legacy +4h/+5h double-ET conversion; current aware timestamps are
    otherwise normalized without reinterpretation.
    """
    candidate_dt = None
    if candidate_id and isinstance(candidate_id, str):
        m = _CANDIDATE_ID_TS_REGEX.search(candidate_id)
        if m:
            date_str, time_str = m.groups()
            try:
                candidate_dt = datetime.strptime(
                    f"{date_str}_{time_str}", "%Y%m%d_%H%M%S"
                ).replace(tzinfo=UTC)
            except (TypeError, ValueError):
                candidate_dt = None

    dt = parse_utc(dt_val)

    if candidate_dt and dt:
        signed_diff = (dt - candidate_dt).total_seconds()
        # Legacy rows could be double-converted from UTC to ET and then persisted as
        # though the result were UTC. Repair only the exact DST offsets with tolerance.
        if abs(signed_diff - 14400) <= 60 or abs(signed_diff - 18000) <= 60:
            return candidate_dt

    return dt or candidate_dt


def classify_shadow_replay_status(
    record: dict[str, Any],
    now: datetime | None = None,
    min_delay_seconds: float = 3600.0,
    max_age_seconds: float = 86400.0 * 7.0,
) -> ReplayEligibilityState:
    """Classify an in-memory shadow record using one aware-UTC time domain."""
    now_utc = ensure_utc(now) if now is not None else utc_now()

    replayed = bool(record.get("replayed", False))
    replay_failed = bool(record.get("replay_failed", False))

    if replayed and not replay_failed:
        return ReplayEligibilityState.SUCCESSFULLY_REPLAYED
    if replay_failed:
        return ReplayEligibilityState.FAILED

    final_decision = str(record.get("final_decision", "")).lower()
    if final_decision != "rejected":
        return ReplayEligibilityState.PENDING_NOT_ELIGIBLE

    ts_val = record.get("ts") or record.get("timestamp")
    dt_ts = parse_datetime_to_utc(ts_val, record.get("candidate_id"))
    if dt_ts is None:
        return ReplayEligibilityState.PENDING_NOT_ELIGIBLE

    age_seconds = (now_utc - dt_ts).total_seconds()
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
    """Generate the SQL predicate for a replay eligibility state.

    SQLite stores legacy timestamps without offsets, so the aware UTC boundary is made
    naive *after* normalization solely at the persistence boundary.  No local timezone
    conversion is performed.
    """
    now_utc = ensure_utc(now) if now is not None else utc_now()
    sql_now = now_utc.replace(tzinfo=None)

    rejected_cond = table_class.final_decision == "rejected"
    mode_cond = table_class.mode == mode
    not_replayed_cond = or_(table_class.replayed == None, table_class.replayed == False)  # noqa: E711
    not_failed_cond = or_(table_class.replay_failed == None, table_class.replay_failed == False)  # noqa: E711

    if state == ReplayEligibilityState.SUCCESSFULLY_REPLAYED:
        return and_(mode_cond, table_class.replayed == True, not_failed_cond)  # noqa: E712
    if state == ReplayEligibilityState.FAILED:
        return and_(mode_cond, table_class.replay_failed == True)  # noqa: E712

    base_pending = and_(mode_cond, rejected_cond, not_replayed_cond, not_failed_cond)
    min_delay_cutoff = sql_now - timedelta(seconds=min_delay_seconds)
    max_age_cutoff = sql_now - timedelta(seconds=max_age_seconds)

    if state == ReplayEligibilityState.ELIGIBLE:
        return and_(base_pending, table_class.ts <= min_delay_cutoff, table_class.ts >= max_age_cutoff)
    if state == ReplayEligibilityState.PENDING_NOT_ELIGIBLE:
        return and_(base_pending, table_class.ts > min_delay_cutoff, table_class.ts >= max_age_cutoff)
    if state == ReplayEligibilityState.EXPIRED_UNREPLAYED:
        return and_(base_pending, table_class.ts < max_age_cutoff)
    return base_pending
