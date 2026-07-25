"""Bounded evolutionary controller for the SHADOW-ONLY spawn laboratory.

The research objective is edge per unit of scarce capital, not gross exposure. Evolution
rewards expectancy and profit generated from small peak capital footprints while penalizing
drawdown, capital intensity, and insufficient evidence. Nothing here has broker authority.
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
    mutation_fraction: float = 0.12
    seed_capital: float = 2500.0
    bootstrap_population: int = 6
    confidence_trade_count: int = 30
    # Capital-efficiency objective: a child should not win merely by consuming more capital.
    capital_intensity_penalty: float = 0.35
    drawdown_linear_penalty: float = 0.15
    drawdown_quadratic_penalty: float = 0.02


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
    peak_capital_pct: float = 0.0
    capital_efficiency: float = 0.0


class SpawnEvolutionEngine:
    """Research-only evolution over candidate, risk, exit, and capital-efficiency dimensions."""

    # Deliberately bounded. The laboratory may test whether a higher cap is useful, but fitness
    # below makes capital consumption costly; exposure by itself is never rewarded.
    PARAM_BOUNDS = {
        ("ml", "ml_threshold"): (0.48, 0.80),
        ("risk", "min_trade_score"): (20.0, 95.0),
        ("risk", "risk_per_trade_pct"): (0.10, 2.0),
        ("risk", "max_position_pct"): (5.0, 45.0),
        ("risk", "trailing_stop_atr_mult"): (0.0, 4.0),
    }

    def __init__(self, settings: EvolutionSettings | None = None, seed: int = 146) -> None:
        self.settings = settings or EvolutionSettings()
        self.rng = random.Random(seed)
        self.generation = 0
        if self.settings.elite_count >= self.settings.max_population:
            raise ValueError("elite_count must be smaller than max_population")
        if self.settings.confidence_trade_count < self.settings.min_trades_for_selection:
            raise ValueError("confidence_trade_count must be >= min_trades_for_selection")

    @staticmethod
    def _peak_capital_pct(portfolio) -> float:
        # SpawnVirtualPortfolio tracks the maximum marked notional actually committed. Older
        # checkpoints do not have it, so fall back to zero rather than inventing utilization.
        peak = float(getattr(portfolio, "peak_capital_deployed", 0.0) or 0.0)
        return (peak / portfolio.seed_capital * 100.0) if portfolio.seed_capital > 0 else 0.0

    def score(self, executor: ChildPolicyExecutor) -> FitnessSnapshot:
        portfolio = executor.portfolio
        trades = int(portfolio.trade_count)
        realized_r = float(portfolio.realized_r)
        expectancy_r = realized_r / trades if trades else 0.0
        drawdown_pct = float(portfolio.get_max_drawdown_pct())
        equity_return_pct = ((portfolio.marked_equity / portfolio.seed_capital) - 1.0) * 100.0 if portfolio.seed_capital else 0.0
        peak_capital_pct = self._peak_capital_pct(portfolio)
        eligible = executor.status == "ACTIVE" and trades >= self.settings.min_trades_for_selection and drawdown_pct <= self.settings.max_drawdown_pct

        confidence = min(1.0, trades / float(self.settings.confidence_trade_count)) if trades else 0.0
        growth_fraction = max(-0.999999, equity_return_pct / 100.0)
        growth_score = math.log1p(growth_fraction) * 10.0
        evidence_adjusted_edge = confidence * expectancy_r

        # Percentage return earned per percentage point of peak capital employed. This makes a
        # 5% return using 10% peak capital preferable to the same 5% return using 40% capital.
        capital_efficiency = equity_return_pct / max(peak_capital_pct, 1.0)
        efficiency_score = confidence * capital_efficiency * 4.0

        # Penalize both portfolio damage and raw capital intensity. The quadratic DD term makes
        # increasingly large drawdowns disproportionately unattractive to evolution.
        drawdown_penalty = (
            self.settings.drawdown_linear_penalty * drawdown_pct
            + self.settings.drawdown_quadratic_penalty * drawdown_pct * drawdown_pct
        )
        capital_penalty = self.settings.capital_intensity_penalty * (peak_capital_pct / 100.0)
        fitness = evidence_adjusted_edge + growth_score + efficiency_score - drawdown_penalty - capital_penalty
        if not eligible:
            fitness = -1_000_000.0 + fitness
        return FitnessSnapshot(
            executor.policy_id, trades, realized_r, expectancy_r, drawdown_pct,
            equity_return_pct, fitness, eligible, peak_capital_pct, capital_efficiency,
        )

    def rank(self, executors: Iterable[ChildPolicyExecutor]) -> list[FitnessSnapshot]:
        return sorted(
            (self.score(executor) for executor in executors),
            key=lambda item: (item.fitness, item.capital_efficiency, item.expectancy_r, item.trades, item.policy_id),
            reverse=True,
        )

    def select_elites(self, executors: dict[str, ChildPolicyExecutor]) -> list[ChildPolicyExecutor]:
        return [executors[item.policy_id] for item in self.rank(executors.values()) if item.eligible][: self.settings.elite_count]

    def bootstrap_configs(self, now: datetime | None = None) -> list[ChildPolicyConfig]:
        now = now or datetime.now(timezone.utc)
        # Small-capital cohorts first. Higher caps exist only as controlled experiments and must
        # overcome the capital-intensity penalty to survive selection.
        seeds = [
            (0.48, 45.0, 0.35, 0.0, 8.0),
            (0.50, 50.0, 0.50, 0.5, 12.0),
            (0.52, 55.0, 0.65, 1.0, 16.0),
            (0.55, 60.0, 0.80, 1.5, 20.0),
            (0.58, 65.0, 0.60, 2.0, 30.0),
            (0.62, 70.0, 0.45, 2.5, 40.0),
        ]
        out = []
        for index, (ml_threshold, score, risk, trail, position_cap) in enumerate(seeds[: self.settings.bootstrap_population]):
            out.append(ChildPolicyConfig(
                policy_id=f"spawn_g0_seed_{index + 1:02d}", parent_policy_id="champion", policy_version="g0",
                spawned_at=now, seed_capital=self.settings.seed_capital, execution_mode="SHADOW_ONLY",
                ml_overrides={"ml_threshold": ml_threshold},
                risk_overrides={
                    "min_trade_score": score, "risk_per_trade_pct": risk,
                    "max_position_pct": position_cap, "trailing_stop_atr_mult": trail,
                    "allow_fractional": True,
                },
                spawn_reason="BOOTSTRAP_CAPITAL_EFFICIENCY_COHORT",
            ))
        return out

    @staticmethod
    def _copy(config):
        return dict(config.strategy_overrides), dict(config.ml_overrides), dict(config.meta_overrides), dict(config.risk_overrides)

    def _mutate_value(self, value, lower, upper):
        span = upper - lower
        return min(upper, max(lower, float(value) + self.rng.uniform(-span * self.settings.mutation_fraction, span * self.settings.mutation_fraction)))

    def mutate(self, parent: ChildPolicyConfig, now: datetime | None = None) -> ChildPolicyConfig:
        now = now or datetime.now(timezone.utc)
        strategy, ml, meta, risk = self._copy(parent)
        defaults = {"ml_threshold": 0.48, "min_trade_score": 50.0, "risk_per_trade_pct": 0.5, "max_position_pct": 15.0, "trailing_stop_atr_mult": 0.0}
        dimensions = list(self.PARAM_BOUNDS)
        mutation_count = 2 if self.rng.random() < 0.70 else 1
        selected = self.rng.sample(dimensions, k=mutation_count)
        mutation_labels = []
        for group, key in selected:
            target = {"ml": ml, "risk": risk}[group]
            lower, upper = self.PARAM_BOUNDS[(group, key)]
            target[key] = round(self._mutate_value(target.get(key, defaults[key]), lower, upper), 6)
            mutation_labels.append(f"{group}.{key}={target[key]}")
        risk["allow_fractional"] = True
        mutation_descriptor = ",".join(mutation_labels)
        raw = f"{parent.policy_id}|{parent.compute_fingerprint()}|{self.generation}|{mutation_descriptor}|{now.isoformat()}"
        child_id = f"spawn_g{self.generation}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        return ChildPolicyConfig(
            policy_id=child_id, parent_policy_id=parent.policy_id, policy_version=f"g{self.generation}", spawned_at=now,
            seed_capital=min(parent.seed_capital, self.settings.seed_capital), execution_mode="SHADOW_ONLY",
            strategy_overrides=strategy, ml_overrides=ml, meta_overrides=meta, risk_overrides=risk,
            code_commit_sha=parent.code_commit_sha, model_manifest_sha=parent.model_manifest_sha,
            spawn_reason=f"EVOLUTION_CAPITAL_EFFICIENCY:{mutation_descriptor}",
        )

    @staticmethod
    def behavior_signature(executor: ChildPolicyExecutor) -> str:
        payload = "|".join(
            f"{trade.get('symbol')}:{trade.get('side')}:{trade.get('entry_at')}:{trade.get('exit_at')}:{trade.get('exit_reason')}"
            for trade in executor.portfolio.closed_trades
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def next_generation(self, executors, now=None):
        elites = self.select_elites(executors)
        if not elites:
            return []
        count = min(max(0, self.settings.max_population - sum(executor.status == "ACTIVE" for executor in executors.values())), self.settings.children_per_cycle)
        if not count:
            return []
        self.generation += 1
        out = []
        seen = {executor.config_fingerprint for executor in executors.values()}
        attempts = 0
        while len(out) < count and attempts < count * 30:
            parent = elites[attempts % len(elites)]
            attempts += 1
            child = self.mutate(parent.config, now)
            fingerprint = child.compute_fingerprint()
            if fingerprint not in seen:
                seen.add(fingerprint)
                out.append(child)
        return out

    def behavior_clone_groups(self, executors: dict[str, ChildPolicyExecutor], min_trades: int = 1) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for executor in executors.values():
            if executor.portfolio.trade_count < min_trades:
                continue
            groups.setdefault(self.behavior_signature(executor), []).append(executor.policy_id)
        return {signature: ids for signature, ids in groups.items() if len(ids) > 1}

    def retirement_candidates(self, executors):
        scored = self.rank(executors.values())
        sampled = [item for item in scored if item.trades >= self.settings.min_trades_for_selection]
        weak = [item.policy_id for item in sampled if item.drawdown_pct > self.settings.max_drawdown_pct or item.expectancy_r < -0.20]
        score_by_id = {item.policy_id: item for item in scored}
        clones = self.behavior_clone_groups(executors, self.settings.min_trades_for_selection)
        for ids in clones.values():
            ordered = sorted(ids, key=lambda policy_id: (score_by_id[policy_id].fitness, score_by_id[policy_id].capital_efficiency, score_by_id[policy_id].trades, policy_id), reverse=True)
            weak.extend(ordered[1:])
        overflow = max(0, sum(executor.status == "ACTIVE" for executor in executors.values()) - self.settings.max_population)
        if overflow:
            weak.extend([item.policy_id for item in reversed(scored) if item.trades >= self.settings.min_trades_for_selection][:overflow])
        return list(dict.fromkeys(weak))