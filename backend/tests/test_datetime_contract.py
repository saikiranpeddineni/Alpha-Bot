from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.shadow_replay_eligibility import (
    ReplayEligibilityState,
    classify_shadow_replay_status,
    parse_datetime_to_utc,
)
from app.core.time_utils import ensure_utc, parse_utc


def test_ensure_utc_normalizes_aware_and_legacy_naive_values() -> None:
    naive = datetime(2026, 7, 24, 13, 30)
    assert ensure_utc(naive) == datetime(2026, 7, 24, 13, 30, tzinfo=UTC)

    plus_four = datetime.fromisoformat("2026-07-24T17:30:00+04:00")
    assert ensure_utc(plus_four) == datetime(2026, 7, 24, 13, 30, tzinfo=UTC)


def test_parse_utc_accepts_z_and_never_returns_naive() -> None:
    parsed = parse_utc("2026-07-24T13:30:00Z")
    assert parsed == datetime(2026, 7, 24, 13, 30, tzinfo=UTC)
    assert parsed.tzinfo is not None


def test_legacy_double_et_shift_repair_is_narrow() -> None:
    candidate = "AAPL_MOMENTUM_20260724_133000_ABC123"
    repaired = parse_datetime_to_utc("2026-07-24T17:30:00", candidate)
    assert repaired == datetime(2026, 7, 24, 13, 30, tzinfo=UTC)

    # A normal one-minute difference is not rewritten from the candidate ID.
    normal = parse_datetime_to_utc("2026-07-24T13:31:00+00:00", candidate)
    assert normal == datetime(2026, 7, 24, 13, 31, tzinfo=UTC)


def test_shadow_replay_eligibility_uses_injected_historical_now() -> None:
    signal_ts = datetime(2026, 7, 24, 13, 30, tzinfo=UTC)
    record = {
        "candidate_id": "AAPL_MOMENTUM_20260724_133000_ABC123",
        "ts": signal_ts.isoformat(),
        "final_decision": "rejected",
    }

    assert classify_shadow_replay_status(
        record, now=signal_ts + timedelta(minutes=59)
    ) == ReplayEligibilityState.PENDING_NOT_ELIGIBLE
    assert classify_shadow_replay_status(
        record, now=signal_ts + timedelta(minutes=60)
    ) == ReplayEligibilityState.ELIGIBLE


def test_shadow_replay_mixed_timezone_inputs_compare_same_instant() -> None:
    record = {
        "ts": "2026-07-24T17:30:00+04:00",
        "final_decision": "rejected",
    }
    now = datetime.fromisoformat("2026-07-24T18:30:00+04:00")
    assert classify_shadow_replay_status(record, now=now) == ReplayEligibilityState.ELIGIBLE
