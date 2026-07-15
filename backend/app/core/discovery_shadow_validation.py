"""Offline aggregation for fixed-vs-dynamic discovery shadow evidence.

This module has no trading authority and performs no provider calls.  It consumes
causally recorded per-session evidence and reports coverage/qualification/cost
metrics without changing scanner decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ShadowSessionEvidence:
    session: str
    fixed_candidates: frozenset[str]
    dynamic_candidates: frozenset[str]
    fixed_qualified: frozenset[str]
    dynamic_qualified: frozenset[str]
    fixed_requests: int
    dynamic_requests: int
    fixed_latency_ms: float
    dynamic_latency_ms: float
    dynamic_missing: int = 0
    dynamic_stale: int = 0

    def __post_init__(self) -> None:
        if not self.session.strip():
            raise ValueError("session is required")
        if min(self.fixed_requests, self.dynamic_requests, self.dynamic_missing, self.dynamic_stale) < 0:
            raise ValueError("counts must be non-negative")
        if self.fixed_latency_ms < 0 or self.dynamic_latency_ms < 0:
            raise ValueError("latency must be non-negative")
        if not self.fixed_qualified <= self.fixed_candidates:
            raise ValueError("fixed qualified symbols must be fixed candidates")
        if not self.dynamic_qualified <= self.dynamic_candidates:
            raise ValueError("dynamic qualified symbols must be dynamic candidates")


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    sessions: int
    fixed_candidate_symbols: int
    dynamic_candidate_symbols: int
    fixed_qualified_symbols: int
    dynamic_qualified_symbols: int
    dynamic_recall_of_fixed_candidates: float
    dynamic_recall_of_fixed_qualified: float
    dynamic_incremental_candidates: int
    dynamic_incremental_qualified: int
    fixed_requests: int
    dynamic_requests: int
    request_delta: int
    fixed_mean_latency_ms: float
    dynamic_mean_latency_ms: float
    latency_delta_ms: float
    dynamic_missing: int
    dynamic_stale: int


def _recall(reference: set[tuple[str, str]], observed: set[tuple[str, str]]) -> float:
    if not reference:
        return 1.0
    return len(reference & observed) / len(reference)


def compare_shadow_sessions(evidence: Iterable[ShadowSessionEvidence]) -> ShadowComparison:
    rows = tuple(evidence)
    if not rows:
        raise ValueError("at least one shadow session is required")
    sessions = [row.session for row in rows]
    if len(sessions) != len(set(sessions)):
        raise ValueError("shadow sessions must be unique")

    fixed_candidates = {(row.session, symbol) for row in rows for symbol in row.fixed_candidates}
    dynamic_candidates = {(row.session, symbol) for row in rows for symbol in row.dynamic_candidates}
    fixed_qualified = {(row.session, symbol) for row in rows for symbol in row.fixed_qualified}
    dynamic_qualified = {(row.session, symbol) for row in rows for symbol in row.dynamic_qualified}
    fixed_requests = sum(row.fixed_requests for row in rows)
    dynamic_requests = sum(row.dynamic_requests for row in rows)
    fixed_latency = mean(row.fixed_latency_ms for row in rows)
    dynamic_latency = mean(row.dynamic_latency_ms for row in rows)

    return ShadowComparison(
        sessions=len(rows),
        fixed_candidate_symbols=len(fixed_candidates),
        dynamic_candidate_symbols=len(dynamic_candidates),
        fixed_qualified_symbols=len(fixed_qualified),
        dynamic_qualified_symbols=len(dynamic_qualified),
        dynamic_recall_of_fixed_candidates=_recall(fixed_candidates, dynamic_candidates),
        dynamic_recall_of_fixed_qualified=_recall(fixed_qualified, dynamic_qualified),
        dynamic_incremental_candidates=len(dynamic_candidates - fixed_candidates),
        dynamic_incremental_qualified=len(dynamic_qualified - fixed_qualified),
        fixed_requests=fixed_requests,
        dynamic_requests=dynamic_requests,
        request_delta=dynamic_requests - fixed_requests,
        fixed_mean_latency_ms=fixed_latency,
        dynamic_mean_latency_ms=dynamic_latency,
        latency_delta_ms=dynamic_latency - fixed_latency,
        dynamic_missing=sum(row.dynamic_missing for row in rows),
        dynamic_stale=sum(row.dynamic_stale for row in rows),
    )
