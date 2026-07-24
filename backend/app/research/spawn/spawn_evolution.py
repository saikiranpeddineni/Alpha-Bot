"""Bounded evolutionary controller for the SHADOW-ONLY spawn laboratory."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
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
    bootstrap_population: int = 6


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
    """Research-only evolution; parameters are limited to evidence the bridge actually supplies."""

    PARAM_BOUNDS = {
        ("ml", "ml_threshold"): (0.48, 0.80),
        ("risk", "min_trade_score"): (20.0, 95.0),
        ("risk", "risk_per_trade_pct"): (0.10, 2.0),
        ("risk", "max_position_pct"): (2.0, 35.0),
        ("risk", "trailing_stop_atr_mult"): (0.0, 4.0),
    }

    def __init__(self, settings: EvolutionSettings | None = None, seed: int = 146) -> None:
        self.settings = settings or EvolutionSettings()
        self.rng = random.Random(seed)
        self.generation = 0
        if self.settings.elite_count >= self.settings.max_population:
            raise ValueError("elite_count must be smaller than max_population")

    def score(self, executor: ChildPolicyExecutor) -> FitnessSnapshot:
        portfolio = executor.portfolio
        trades = int(portfolio.trade_count)
        realized_r = float(portfolio.realized_r)
        expectancy_r = realized_r / trades if trades else 0.0
        # Selection and retirement must use the worst historical drawdown, not
        # merely the drawdown at the instant fitness happens to be sampled.
        drawdown_pct = float(portfolio.get_max_drawdown_pct())
        equity_return_pct = ((portfolio.marked_equity / portfolio.seed_capital) - 1.0) * 100.0 if portfolio.seed_capital else 0.0
        eligible = (
            executor.status == "ACTIVE"
            and trades >= self.settings.min_trades_for_selection
            and drawdown_pct <= self.settings.max_drawdown_pct
        )
        fitness = expectancy_r - 0.05 * drawdown_pct + 0.01 * equity_return_pct
        if not eligible:
            fitness = -1_000_000.0 + fitness
        return FitnessSnapshot(
            executor.policy_id,
            trades,
            realized_r,
            expectancy_r,
            drawdown_pct,
            equity_return_pct,
            fitness,
            eligible,
        )

    def rank(self, executors: Iterable[ChildPolicyExecutor]) -> list[FitnessSnapshot]:
        return sorted(
            (self.score(executor) for executor in executors),
            key=lambda item: (item.fitness, item.trades, item.policy_id),
            reverse=True,
        )

    def select_elites(self, executors: dict[str, ChildPolicyExecutor]) -> list[ChildPolicyExecutor]:
        return [executors[item.policy_id] for item in self.rank(executors.values()) if item.eligible][: self.settings.elite_count]

    def bootstrap_configs(self, now: datetime | None = None) -> list[ChildPolicyConfig]:
        now = now or datetime.now(timezone.utc)
        seeds = [
            (0.48, 45.0, 0.5, 0.0),
            (0.50, 50.0, 0.7, 0.5),
            (0.52, 55.0, 0.8, 1.0),
            (0.55, 60.0, 0.6, 1.5),
            (0.58, 65.0, 0.5, 2.0),
            (0.62, 70.0, 0.4, 2.5),
        ]
        out = []
        for index, (ml_threshold, score, risk, trail) in enumerate(seeds[: self.settings.bootstrap_population]):
            out.append(
                ChildPolicyConfig(
                    policy_id=f"spawn_g0_seed_{index + 1:02d}",
                    parent_policy_id="champion",
                    policy_version="g0",
                    spawned_at=now,
                    seed_capital=self.settings.seed_capital,
                    execution_mode="SHADOW_ONLY",
                    ml_overrides={"ml_threshold": ml_threshold},
                    risk_overrides={
                        "min_trade_score": score,
                        "risk_per_trade_pct": risk,
                        "max_position_pct": 20.0,
                        "trailing_stop_atr_mult": trail,
                    },
                    spawn_reason="BOOTSTRAP_DIVERSE_RISK_EXIT_COHORT",
                )
            )
        return out

    @staticmethod
    def _copy(config):
        return (
            dict(config.strategy_overrides),
            dict(config.ml_overrides),
            dict(config.meta_overrides),
            dict(config.risk_overrides),
        )

    def _mutate_value(self, value, lower, upper):
        span = upper - lower
        return min(upper, max(lower, float(value) + self.rng.uniform(-span * self.settings.mutation_fraction, span * self.settings.mutation_fraction)))

    def mutate(self, parent: ChildPolicyConfig, now: datetime | None = None) -> ChildPolicyConfig:
        now = now or datetime.now(timezone.utc)
        strategy, ml, meta, risk = self._copy(parent)
        group, key = self.rng.choice(list(self.PARAM_BOUNDS))
        target = {"ml": ml, "risk": risk}[group]
        lower, upper = self.PARAM_BOUNDS[(group, key)]
        defaults = {
            "ml_threshold": 0.48,
            "min_trade_score": 50.0,
            "risk_per_trade_pct": 1.0,
            "max_position_pct": 25.0,
            "trailing_stop_atr_mult": 0.0,
        }
        target[key] = round(self._mutate_value(target.get(key, defaults[key]), lower, upper), 6)
        raw = f"{parent.policy_id}|{parent.compute_fingerprint()}|{self.generation}|{key}|{target[key]}|{now.isoformat()}"
        child_id = f"spawn_g{self.generation}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        return ChildPolicyConfig(
            policy_id=child_id,
            parent_policy_id=parent.policy_id,
            policy_version=f"g{self.generation}",
            spawned_at=now,
            seed_capital=min(parent.seed_capital, self.settings.seed_capital),
            execution_mode="SHADOW_ONLY",
            strategy_overrides=strategy,
            ml_overrides=ml,
            meta_overrides=meta,
            risk_overrides=risk,
            code_commit_sha=parent.code_commit_sha,
            model_manifest_sha=parent.model_manifest_sha,
            spawn_reason=f"EVOLUTION_MUTATION:{group}.{key}",
        )

    def next_generation(self, executors, now=None):
        elites = self.select_elites(executors)
        if not elites:
            return []
        count = min(
            max(0, self.settings.max_population - sum(executor.status == "ACTIVE" for executor in executors.values())),
            self.settings.children_per_cycle,
        )
        if not count:
            return []
        self.generation += 1
        out = []
        seen = {executor.config_fingerprint for executor in executors.values()}
        attempts = 0
        while len(out) < count and attempts < count * 20:
            parent = elites[attempts % len(elites)]
            attempts += 1
            child = self.mutate(parent.config, now)
            fingerprint = child.compute_fingerprint()
            if fingerprint not in seen:
                seen.add(fingerprint)
                out.append(child)
        return out

    def retirement_candidates(self, executors):
        scored = self.rank(executors.values())
        sampled = [item for item in scored if item.trades >= self.settings.min_trades_for_selection]
        weak = [
            item.policy_id
            for item in sampled
            if item.drawdown_pct > self.settings.max_drawdown_pct or item.expectancy_r < -0.20
        ]
        overflow = max(0, sum(executor.status == "ACTIVE" for executor in executors.values()) - self.settings.max_population)
        if overflow:
            weak.extend(
                [item.policy_id for item in reversed(scored) if item.trades >= self.settings.min_trades_for_selection][:overflow]
            )
        return list(dict.fromkeys(weak))
