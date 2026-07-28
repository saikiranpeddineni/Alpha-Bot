"""Evidence-driven mutation memory for the SHADOW-ONLY Spawn laboratory.

The memory learns which bounded parameter moves have historically improved child
fitness relative to their parent.  It deliberately has no broker/database access
and never claims future profitability; it only biases exploration using observed
parent-relative evidence while retaining a non-zero exploration probability.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Iterable


@dataclass(frozen=True)
class MutationOutcome:
    dimension: str
    direction: int
    fitness_delta: float
    child_trades: int


@dataclass(frozen=True)
class MutationPreference:
    dimension: str
    direction: int
    samples: int
    mean_delta: float
    confidence: float
    weight: float


class SpawnMutationIntelligence:
    def __init__(self, min_samples: int = 3, prior_strength: float = 5.0, exploration_floor: float = 0.20) -> None:
        self.min_samples = max(1, int(min_samples))
        self.prior_strength = max(1.0, float(prior_strength))
        self.exploration_floor = min(1.0, max(0.05, float(exploration_floor)))
        self._outcomes: list[MutationOutcome] = []

    def record(self, dimension: str, direction: int, fitness_delta: float, child_trades: int) -> None:
        if not dimension or direction not in (-1, 1) or not math.isfinite(float(fitness_delta)):
            return
        self._outcomes.append(MutationOutcome(str(dimension), int(direction), float(fitness_delta), max(0, int(child_trades))))

    def preferences(self) -> list[MutationPreference]:
        grouped: dict[tuple[str, int], list[MutationOutcome]] = {}
        for outcome in self._outcomes:
            grouped.setdefault((outcome.dimension, outcome.direction), []).append(outcome)
        prefs: list[MutationPreference] = []
        for (dimension, direction), rows in grouped.items():
            n = len(rows)
            raw_mean = sum(row.fitness_delta for row in rows) / n
            confidence = min(1.0, n / max(float(self.min_samples), self.prior_strength))
            shrunk_mean = raw_mean * (n / (n + self.prior_strength))
            # Negative evidence never removes exploration completely.
            evidence_weight = max(0.05, 1.0 + shrunk_mean)
            prefs.append(MutationPreference(dimension, direction, n, shrunk_mean, confidence, evidence_weight))
        return prefs

    def choose(self, dimensions: Iterable[tuple[str, str]], rng: random.Random) -> tuple[tuple[str, str], int]:
        available = list(dimensions)
        if not available:
            raise ValueError("at least one mutation dimension is required")
        if rng.random() < self.exploration_floor or not self._outcomes:
            return rng.choice(available), rng.choice((-1, 1))
        pref_map = {(p.dimension, p.direction): p for p in self.preferences()}
        candidates: list[tuple[tuple[str, str], int]] = []
        weights: list[float] = []
        for group, key in available:
            dimension = f"{group}.{key}"
            for direction in (-1, 1):
                pref = pref_map.get((dimension, direction))
                candidates.append(((group, key), direction))
                weights.append(1.0 if pref is None else max(0.05, pref.weight * (0.5 + pref.confidence)))
        return rng.choices(candidates, weights=weights, k=1)[0]
