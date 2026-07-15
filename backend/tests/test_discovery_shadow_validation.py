import pytest

from app.core.discovery_shadow_validation import (
    ShadowSessionEvidence,
    compare_shadow_sessions,
)


def _row(session, fixed, dynamic, fixed_q, dynamic_q, fixed_requests, dynamic_requests, fixed_ms, dynamic_ms, missing=0, stale=0):
    return ShadowSessionEvidence(
        session=session,
        fixed_candidates=frozenset(fixed),
        dynamic_candidates=frozenset(dynamic),
        fixed_qualified=frozenset(fixed_q),
        dynamic_qualified=frozenset(dynamic_q),
        fixed_requests=fixed_requests,
        dynamic_requests=dynamic_requests,
        fixed_latency_ms=fixed_ms,
        dynamic_latency_ms=dynamic_ms,
        dynamic_missing=missing,
        dynamic_stale=stale,
    )


def test_shadow_comparison_preserves_session_identity_and_reports_recall_cost_and_coverage():
    report = compare_shadow_sessions(
        [
            _row("2026-06-03", {"A", "B"}, {"B", "X"}, {"B"}, {"B", "X"}, 8, 3, 80, 30, missing=2),
            _row("2026-06-17", {"A", "C"}, {"A", "C", "Y"}, {"C"}, {"C", "Y"}, 7, 4, 100, 50, stale=1),
            _row("2026-07-01", {"A"}, {"Z"}, set(), {"Z"}, 9, 2, 120, 40),
        ]
    )

    assert report.sessions == 3
    assert report.fixed_candidate_symbols == 5
    assert report.dynamic_candidate_symbols == 6
    assert report.dynamic_recall_of_fixed_candidates == pytest.approx(3 / 5)
    assert report.dynamic_recall_of_fixed_qualified == 1.0
    assert report.dynamic_incremental_candidates == 3
    assert report.dynamic_incremental_qualified == 3
    assert report.fixed_requests == 24
    assert report.dynamic_requests == 9
    assert report.request_delta == -15
    assert report.fixed_mean_latency_ms == 100
    assert report.dynamic_mean_latency_ms == 40
    assert report.latency_delta_ms == -60
    assert report.dynamic_missing == 2
    assert report.dynamic_stale == 1


def test_same_symbol_on_different_sessions_is_not_collapsed():
    report = compare_shadow_sessions(
        [
            _row("s1", {"A"}, {"A"}, {"A"}, {"A"}, 1, 1, 1, 1),
            _row("s2", {"A"}, set(), {"A"}, set(), 1, 1, 1, 1),
        ]
    )
    assert report.fixed_candidate_symbols == 2
    assert report.dynamic_recall_of_fixed_candidates == 0.5
    assert report.dynamic_recall_of_fixed_qualified == 0.5


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"session": " ", "fixed_candidates": frozenset({"A"}), "dynamic_candidates": frozenset({"A"}), "fixed_qualified": frozenset(), "dynamic_qualified": frozenset(), "fixed_requests": 1, "dynamic_requests": 1, "fixed_latency_ms": 1, "dynamic_latency_ms": 1}, "session is required"),
        ({"session": "s", "fixed_candidates": frozenset({"A"}), "dynamic_candidates": frozenset({"A"}), "fixed_qualified": frozenset({"A"}), "dynamic_qualified": frozenset({"A"}), "fixed_requests": -1, "dynamic_requests": 1, "fixed_latency_ms": 1, "dynamic_latency_ms": 1}, "counts must be non-negative"),
        ({"session": "s", "fixed_candidates": frozenset({"A"}), "dynamic_candidates": frozenset({"A"}), "fixed_qualified": frozenset({"B"}), "dynamic_qualified": frozenset({"A"}), "fixed_requests": 1, "dynamic_requests": 1, "fixed_latency_ms": 1, "dynamic_latency_ms": 1}, "fixed qualified symbols must be fixed candidates"),
    ],
)
def test_invalid_shadow_evidence_fails_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ShadowSessionEvidence(**kwargs)


def test_duplicate_or_empty_session_sets_are_rejected():
    row = _row("s1", {"A"}, {"A"}, set(), set(), 1, 1, 1, 1)
    with pytest.raises(ValueError, match="at least one"):
        compare_shadow_sessions([])
    with pytest.raises(ValueError, match="unique"):
        compare_shadow_sessions([row, row])
