"""Bounded evolutionary controller for the SHADOW-ONLY spawn laboratory.

This module can create and rank research descendants indefinitely while enforcing
population, diversity, mutation, drawdown and sample-size constraints. It has no
broker imports and no production promotion authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import random
from typing import Iterable

from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_executor import ChildPolicyExecutor


@dataclass(frozen=True)
class EvolutionSettings:
    max_population: int = 24
    elite_count: int = 6
    children_per_cycle: int = 6
    min_trades_for_selection: int = 12
    max_drawdown_pct: float = 12.0
    mutation_fraction: float = 0.08
    seed_capital: float = 2500.0


@dataclass(frozen=True)
class FitnessSnapshot:
    policy_id: str
    trades: int
    realized_r: float
    expectancy_r: float
    drawdown_pct: float
    equity_return_pct: float
    fitness: float
    eligible: bool


class SpawnEvolutionEngine:
    """Deterministic, bounded research evolution. Never executes real orders."""
    PARAM_BOUNDS = {
        ("ml", "ml_threshold"): (0.30, 0.80),
        ("meta", "meta_threshold"): (0.30, 0.85),
        ("risk", "min_trade_score"): (20.0, 95.0),
        ("risk", "risk_per_trade_pct"): (0.10, 2.00),
        ("risk", "max_position_pct"): (2.0, 35.0),
        ("risk", "trailing_stop_atr_mult"): (0.0, 4.0),
    }

    def __init__(self, settings: EvolutionSettings | None = None, seed: int = 146) -> None:
        self.settings = settings or EvolutionSettings()
        if self.settings.elite_count >= self.settings.max_population:
            raise ValueError("elite_count must be smaller than max_population")
        self.rng = random.Random(seed)
        self.generation = 0

    def score(self, executor: ChildPolicyExecutor) -> FitnessSnapshot:
        p = executor.portfolio
        trades = int(p.trade_count)
        realized_r = float(p.realized_r)
        expectancy = realized_r / trades if trades else 0.0
        dd = float(p.get_drawdown_pct())
        ret = ((p.marked_equity / p.seed_capital) - 1.0) * 100.0 if p.seed_capital else 0.0
        eligible = executor.status == "ACTIVE" and trades >= self.settings.min_trades_for_selection and dd <= self.settings.max_drawdown_pct
        # Profit alone is deliberately insufficient: expectancy and drawdown dominate,
        # while a smooth positive equity return receives a small secondary reward.
        fitness = expectancy - 0.05 * dd + 0.01 * ret
        if not eligible:
            fitness = -1_000_000.0 + fitness
        return FitnessSnapshot(executor.policy_id, trades, realized_r, expectancy, dd, ret, fitness, eligible)

    def rank(self, executors: Iterable[ChildPolicyExecutor]) -> list[FitnessSnapshot]:
        return sorted((self.score(e) for e in executors), key=lambda x: (x.fitness, x.trades, x.policy_id), reverse=True)

    def select_elites(self, executors: dict[str, ChildPolicyExecutor]) -> list[ChildPolicyExecutor]:
        ranked = [x for x in self.rank(executors.values()) if x.eligible]
        return [executors[x.policy_id] for x in ranked[: self.settings.elite_count]]

    @staticmethod
    def _copy_overrides(cfg: ChildPolicyConfig) -> tuple[dict, dict, dict, dict]:
        return (dict(cfg.strategy_overrides), dict(cfg.ml_overrides), dict(cfg.meta_overrides), dict(cfg.risk_overrides))

    def _mutate_value(self, value: float, lo: float, hi: float) -> float:
        span = hi - lo
        delta = self.rng.uniform(-span * self.settings.mutation_fraction, span * self.settings.mutation_fraction)
        return min(hi, max(lo, float(value) + delta))

    def mutate(self, parent: ChildPolicyConfig, now: datetime | None = None) -> ChildPolicyConfig:
        now = now or datetime.now(timezone.utc)
        strategy, ml, meta, risk = self._copy_overrides(parent)
        choices = list(self.PARAM_BOUNDS)
        group, key = self.rng.choice(choices)
        target = {"ml": ml, "meta": meta, "risk": risk}[group]
        lo, hi = self.PARAM_BOUNDS[(group, key)]
        defaults = {"ml_threshold": 0.48, "meta_threshold": 0.55, "min_trade_score": 50.0,
                    "risk_per_trade_pct": 1.0, "max_position_pct": 25.0, "trailing_stop_atr_mult": 0.0}
        target[key] = round(self._mutate_value(float(target.get(key, defaults[key])), lo, hi), 6)
        raw = f"{parent.policy_id}|{parent.compute_fingerprint()}|{self.generation}|{key}|{target[key]}|{now.isoformat()}"
        child_id = f"spawn_g{self.generation + 1}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        return ChildPolicyConfig(
            policy_id=child_id, parent_policy_id=parent.policy_id, policy_version=f"g{self.generation + 1}",
            spawned_at=now, seed_capital=min(parent.seed_capital, self.settings.seed_capital), execution_mode="SHADOW_ONLY",
            strategy_overrides=strategy, ml_overrides=ml, meta_overrides=meta, risk_overrides=risk,
            code_commit_sha=parent.code_commit_sha, model_manifest_sha=parent.model_manifest_sha,
            spawn_reason=f"EVOLUTION_MUTATION:{group}.{key}",
        )

    def next_generation(self, executors: dict[str, ChildPolicyExecutor], now: datetime | None = None) -> list[ChildPolicyConfig]:
        """Return new descendants only. Caller owns persistence/lifecycle decisions."""
        elites = self.select_elites(executors)
        if not elites:
            return []
        room = max(0, self.settings.max_population - len([e for e in executors.values() if e.status == "ACTIVE"]))
        count = min(room, self.settings.children_per_cycle)
        if count <= 0:
            return []
        self.generation += 1
        children: list[ChildPolicyConfig] = []
        seen = {e.config_fingerprint for e in executors.values()}
        attempts = 0
        while len(children) < count and attempts < count * 20:
            attempts += 1
            parent = elites[(attempts - 1) % len(elites)]
            child = self.mutate(parent.config, now=now)
            fp = child.compute_fingerprint()
            if fp in seen:
                continue
            seen.add(fp)
            children.append(child)
        return children

    def retirement_candidates(self, executors: dict[str, ChildPolicyExecutor]) -> list[str]:
        """Retire only sufficiently sampled, clearly weak policies; never retire on tiny samples."""
        scored = self.rank(executors.values())
        eligible_sample = [x for x in scored if x.trades >= self.settings.min_trades_for_selection]
        weak = [x.policy_id for x in eligible_sample if x.drawdown_pct > self.settings.max_drawdown_pct or x.expectancy_r < -0.20]
        active = [e for e in executors.values() if e.status == "ACTIVE"]
        overflow = max(0, len(active) - self.settings.max_population)
        if overflow:
            worst = [x.policy_id for x in reversed(scored) if x.trades >= self.settings.min_trades_for_selection]
            weak.extend(worst[:overflow])
        return list(dict.fromkeys(weak))
