from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import PositionObservation


@dataclass(frozen=True)
class HealthResult:
    score: float | None
    components: Mapping[str, float]
    coverage: float


class TradeHealthScorer:
    """Deterministic, explainable PIS v1 health score.

    Missing evidence is excluded and weights are renormalized. It is never
    silently converted to bullish or bearish evidence.
    """

    DEFAULT_WEIGHTS = {
        "trend": 0.30,
        "momentum": 0.20,
        "volume": 0.15,
        "relative_strength": 0.15,
        "market_context": 0.20,
    }

    def __init__(self, weights: Mapping[str, float] | None = None, min_coverage: float = 0.50):
        self.weights = dict(weights or self.DEFAULT_WEIGHTS)
        self.min_coverage = float(min_coverage)

    @staticmethod
    def _bound(value: float) -> float:
        return max(0.0, min(100.0, float(value)))

    def score(self, observation: PositionObservation) -> HealthResult:
        components: dict[str, float] = {}
        available_weight = 0.0
        total_weight = sum(self.weights.values()) or 1.0
        weighted_sum = 0.0
        for name, weight in self.weights.items():
            raw = getattr(observation, name, None)
            if raw is None:
                continue
            value = self._bound(raw)
            components[name] = value
            weighted_sum += value * weight
            available_weight += weight
        coverage = available_weight / total_weight
        if available_weight <= 0.0 or coverage < self.min_coverage:
            return HealthResult(score=None, components=components, coverage=coverage)
        return HealthResult(
            score=round(weighted_sum / available_weight, 4),
            components=components,
            coverage=round(coverage, 4),
        )
