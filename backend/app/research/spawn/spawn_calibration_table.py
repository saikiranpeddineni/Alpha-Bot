"""Causal empirical EV calibration for Spawn Commander V4 research.

This module is deliberately broker-free and database-agnostic. Callers provide
historical observations whose timestamps are strictly before the discovery
cutoff. The table applies hierarchical shrinkage so sparse buckets cannot
masquerade as precise alpha.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DISCOVERY_CUTOFF = datetime(2026, 7, 23, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CalibrationEstimate:
    expected_net_dollars: float
    lower_confidence_net_dollars: float
    sample_count: int
    confidence: float
    expected_holding_mins: float
    source_level: str


@dataclass(frozen=True)
class _Stats:
    mean: float
    variance: float
    count: int
    hold_mean: float


def _norm(value: Any, default: str = "unknown") -> str:
    text = str(value if value is not None else default).strip().lower()
    return text or default


def _bucket(value: Any, width: float, default: str = "unknown") -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if width <= 0:
        return default
    lo = int(x / width) * width
    hi = lo + width
    return f"{lo:.3f}:{hi:.3f}"


class SpawnCalibrationTable:
    """Hierarchically-shrunk causal candidate EV lookup.

    Expected input fields:
      ts, strategy, side/direction, regime, forward_net_dollars,
      realized_holding_mins; optional score/trade_score and ml_probability.

    Rows at or after ``cutoff`` are rejected, not silently ignored, so a
    discovery/holdout leak fails loudly during research validation.
    """

    def __init__(
        self,
        cutoff: datetime = DISCOVERY_CUTOFF,
        prior_strength: float = 20.0,
        min_samples: int = 8,
        confidence_z: float = 1.0,
    ) -> None:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        self.cutoff = cutoff
        self.prior_strength = max(1.0, float(prior_strength))
        self.min_samples = max(1, int(min_samples))
        self.confidence_z = max(0.0, float(confidence_z))
        self._rows: List[Mapping[str, Any]] = []
        self._stats: Dict[Tuple[str, ...], _Stats] = {}
        self._global = _Stats(0.0, 0.0, 0, 30.0)

    @staticmethod
    def _timestamp(row: Mapping[str, Any]) -> datetime:
        ts = row.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if not isinstance(ts, datetime) or ts.tzinfo is None:
            raise ValueError("calibration row requires timezone-aware ts")
        return ts

    @staticmethod
    def _stats_for(rows: Sequence[Mapping[str, Any]]) -> _Stats:
        vals = [float(r["forward_net_dollars"]) for r in rows]
        holds = [max(1.0, float(r.get("realized_holding_mins", 30.0) or 30.0)) for r in rows]
        n = len(vals)
        if not n:
            return _Stats(0.0, 0.0, 0, 30.0)
        mean = sum(vals) / n
        variance = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
        return _Stats(mean, variance, n, sum(holds) / n)

    @staticmethod
    def _keys(row: Mapping[str, Any]) -> List[Tuple[str, ...]]:
        strategy = _norm(row.get("strategy"))
        side = _norm(row.get("side", row.get("direction", "long")))
        regime = _norm(row.get("regime"))
        score = row.get("score", row.get("trade_score"))
        ml = row.get("ml_probability")
        score_bucket = _bucket(score, 0.05)
        ml_bucket = _bucket(ml, 0.05)
        return [
            ("specific", strategy, side, regime, score_bucket, ml_bucket),
            ("regime", strategy, side, regime),
            ("side", strategy, side),
            ("strategy", strategy),
            ("global",),
        ]

    def fit(self, rows: Iterable[Mapping[str, Any]]) -> "SpawnCalibrationTable":
        materialized: List[Mapping[str, Any]] = []
        for row in rows:
            ts = self._timestamp(row)
            if ts >= self.cutoff:
                raise ValueError(f"calibration leakage: {ts.isoformat()} >= {self.cutoff.isoformat()}")
            if row.get("forward_net_dollars") is None:
                continue
            materialized.append(row)
        if not materialized:
            raise ValueError("no eligible pre-cutoff calibration rows")

        self._rows = materialized
        self._global = self._stats_for(materialized)
        grouped: Dict[Tuple[str, ...], List[Mapping[str, Any]]] = {}
        for row in materialized:
            for key in self._keys(row):
                grouped.setdefault(key, []).append(row)
        self._stats = {key: self._stats_for(group) for key, group in grouped.items()}
        return self

    def _shrink(self, stats: _Stats, prior: _Stats) -> CalibrationEstimate:
        n = stats.count
        weight = n / (n + self.prior_strength)
        mean = weight * stats.mean + (1.0 - weight) * prior.mean
        variance = weight * stats.variance + (1.0 - weight) * prior.variance
        stderr = sqrt(max(0.0, variance) / max(1, n))
        lower = mean - self.confidence_z * stderr
        confidence = min(1.0, n / max(float(self.min_samples), self.prior_strength))
        hold = weight * stats.hold_mean + (1.0 - weight) * prior.hold_mean
        return CalibrationEstimate(mean, lower, n, confidence, max(1.0, hold), "")

    def estimate(self, observation: Mapping[str, Any]) -> Optional[CalibrationEstimate]:
        if not self._stats:
            raise RuntimeError("calibration table is not fitted")
        prior = self._global
        for key in reversed(self._keys(observation)):
            stats = self._stats.get(key)
            if stats is None:
                continue
            estimate = self._shrink(stats, prior)
            prior = _Stats(estimate.expected_net_dollars, stats.variance, max(1, stats.count), estimate.expected_holding_mins)

        # Select the most-specific bucket that has enough evidence; otherwise
        # return a shrunk broader estimate with explicitly low confidence.
        for key in self._keys(observation):
            stats = self._stats.get(key)
            if stats is None:
                continue
            est = self._shrink(stats, prior)
            return CalibrationEstimate(
                est.expected_net_dollars,
                est.lower_confidence_net_dollars,
                est.sample_count,
                est.confidence,
                est.expected_holding_mins,
                key[0],
            )
        return None

    def enrich(self, observation: Mapping[str, Any], min_confidence: float = 0.25) -> Dict[str, Any]:
        enriched = dict(observation)
        estimate = self.estimate(observation)
        if estimate is None or estimate.confidence < min_confidence:
            enriched["calibration_available"] = False
            enriched["expectancy_confidence"] = 0.0 if estimate is None else estimate.confidence
            return enriched
        # Conservative lower-bound EV is intentional: V4 should demand evidence
        # before consuming scarce capital rather than optimize discovery P&L.
        enriched["calibration_available"] = True
        enriched["calibrated_expected_net_dollars"] = estimate.lower_confidence_net_dollars
        enriched["calibrated_mean_net_dollars"] = estimate.expected_net_dollars
        enriched["expectancy_confidence"] = estimate.confidence
        enriched["expected_holding_mins"] = estimate.expected_holding_mins
        enriched["calibration_source_level"] = estimate.source_level
        enriched["calibration_sample_count"] = estimate.sample_count
        return enriched
