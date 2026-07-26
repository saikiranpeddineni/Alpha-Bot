from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.research.candidate_identity import canonical_candidate_id, canonical_event_id


def test_candidate_id_is_deterministic_across_timezone_representation():
    utc_ts = datetime(2026, 7, 24, 14, 30, tzinfo=UTC)
    local_ts = utc_ts.astimezone(timezone(timedelta(hours=-4)))
    left = canonical_candidate_id(symbol="nvda", strategy="momentum", side="LONG", decision_ts=utc_ts)
    right = canonical_candidate_id(symbol="NVDA", strategy="MOMENTUM", side="long", decision_ts=local_ts)
    assert left == right


def test_candidate_ordinal_disambiguates_same_bar_candidates():
    ts = datetime(2026, 7, 24, 14, 30, tzinfo=UTC)
    first = canonical_candidate_id(symbol="NVDA", strategy="momentum", side="long", decision_ts=ts, ordinal=0)
    second = canonical_candidate_id(symbol="NVDA", strategy="momentum", side="long", decision_ts=ts, ordinal=1)
    assert first != second


def test_identity_rejects_naive_timestamps():
    with pytest.raises(ValueError):
        canonical_candidate_id(symbol="NVDA", strategy="momentum", side="long", decision_ts=datetime(2026, 7, 24, 14, 30))


def test_event_id_is_stable_and_candidate_scoped():
    ts = datetime(2026, 7, 24, 14, 31, tzinfo=UTC)
    a = canonical_event_id(candidate_id="CAND-A", event_type="CANDIDATE", ts=ts, symbol="NVDA")
    b = canonical_event_id(candidate_id="CAND-A", event_type="CANDIDATE", ts=ts, symbol="NVDA")
    c = canonical_event_id(candidate_id="CAND-B", event_type="CANDIDATE", ts=ts, symbol="NVDA")
    assert a == b
    assert a != c
