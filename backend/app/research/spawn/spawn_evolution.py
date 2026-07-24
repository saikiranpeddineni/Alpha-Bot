"""Bounded evolutionary controller for the SHADOW-ONLY spawn laboratory.

The objective is research quality, not a requested backtest return. Evolution rewards
capital-normalized growth and expectancy while explicitly penalizing drawdown and
insufficient evidence. Nothing in this module has broker authority.
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
    # Require more evidence before a policy receives the full fitness benefit.
    confidence_trade_count: int = 30


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
    """Research-only evolution over evidence-backed candidate/risk/exit dimensions."""

    # These are laboratory bounds, not production recommendations. The wider position
    # range lets replay measure whether unused capital contains genuine edge. Drawdown
    # and evidence penalties below prevent raw exposure from automatically winning.
    PARAM_BOUNDS = {
        ("ml", "ml_threshold"): (0.48, 0.80),
        ("risk", "min_trade_score"): (20.0, 95.0),
        ("risk", "risk_per_trade_pct"): (0.10, 3.0),
        ("risk", "max_position_pct"): (5.0, 60.0),
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

    def score(self, executor: ChildPolicyExecutor) -> FitnessSnapshot:
        portfolio = executor.portfolio
        trades = int(portfolio.trade_count)
        realized_r = float(portfolio.realized_r)
        expectancy_r = realized_r / trades if trades else 0.0
        drawdown_pct = float(portfolio.get_max_drawdown_pct())
        equity_return_pct = ((portfolio.marked_equity / portfolio.seed_capital) - 1.0) * 100.0 if portfolio.seed_capital else 0.0
        eligible = executor.status == "ACTIVE" and trades >= self.settings.min_trades_for_selection and drawdown_pct <= self.settings.max_drawdown_pct

        # Sample confidence rises smoothly from the selection floor to the configured
        # evidence target. It prevents a spectacular 12-trade child from automatically
        # outranking a similarly strong, much better observed policy.
        confidence = min(1.0, trades / float(self.settings.confidence_trade_count)) if trades else 0.0

        # Growth is useful only when it survives drawdown. log1p keeps very large replay
        # returns from dominating linearly and works symmetrically for losses > -100%.
        growth_fraction = max(-0.999999, equity_return_pct / 100.0)
        growth_score = math.log1p(growth_fraction) * 10.0
        drawdown_penalty = 0.12 * drawdown_pct + 0.015 * drawdown_pct * drawdown_pct
        evidence_adjusted_edge = confidence * expectancy_r
        fitness = evidence_adjusted_edge + growth_score - drawdown_penalty
        if not eligible:
            fitness = -1_000_000.0 + fitness
        return FitnessSnapshot(executor.policy_id, trades, realized_r, expectancy_r, drawdown_pct, equity_return_pct, fitness, eligible)

    def rank(self, executors: Iterable[ChildPolicyExecutor]) -> list[FitnessSnapshot]:
        return sorted((self.score(executor) for executor in executors), key=lambda item: (item.fitness, item.trades, item.policy_id), reverse=True)

    def select_elites(self, executors: dict[str, ChildPolicyExecutor]) -> list[ChildPolicyExecutor]:
        return [executors[item.policy_id] for item in self.rank(executors.values()) if item.eligible][: self.settings.elite_count]

    def bootstrap_configs(self, now: datetime | None = None) -> list[ChildPolicyConfig]:
        now = now or datetime.now(timezone.utc)
        # Seed different capital-utilization regimes from day one instead of forcing all
        # six policies through the same 20% position ceiling.
        seeds = [
            (0.48, 45.0, 0.5, 0.0, 15.0),
            (0.50, 50.0, 0.7, 0.5, 25.0),
            (0.52, 55.0, 0.8, 1.0, 35.0),
            (0.55, 60.0, 0.6, 1.5, 45.0),
            (0.58, 65.0, 0.5, 2.0, 55.0),
            (0.62, 70.0, 0.4, 2.5, 60.0),
        ]
        out = []
        for index, (ml_threshold, score, risk, trail, position_cap) in enumerate(seeds[: self.settings.bootstrap_population]):
            out.append(ChildPolicyConfig(
                policy_id=f"spawn_g0_seed_{index + 1:02d}", parent_policy_id="champion", policy_version="g0",
                spawned_at=now, seed_capital=self.settings.seed_capital, execution_mode="SHADOW_ONLY",
                ml_overrides={"ml_threshold": ml_threshold},
                risk_overrides={
                    "min_trade_score": score, "risk_per_trade_pct": risk, "max_position_pct": position_cap,
                    "trailing_stop_atr_mult": trail, "allow_fractional": True,
                },
                spawn_reason="BOOTSTRAP_DIVERSE_CAPITAL_RISK_EXIT_COHORT",
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
        defaults = {"ml_threshold": 0.48, "min_trade_score": 50.0, "risk_per_trade_pct": 1.0, "max_position_pct": 25.0, "trailing_stop_atr_mult": 0.0}

        # Most children alter two independent dimensions. This reduces dead mutations
        # where a tiny threshold move never crosses a candidate decision boundary.
        dimensions = list(self.PARAM_BOUNDS)
        mutation_count = 2 if self.rng.random() < 0.70 else 1
        selected = self.rng.sample(dimensions, k=mutation_count)
        mutation_labels = []
        for group, key in selected:
            target = {"ml": ml, "risk": risk}[group]
            lower, upper = self.PARAM_BOUNDS[(group, key)]
            target[key] = round(self._mutate_value(target.get(key, defaults[key]), lower, upper), 6)
            mutation_labels.append(f"{group}.{key}={target[key]}")

        # Fractional sizing is a simulation-fidelity invariant, never an evolutionary
        # dimension and never connected to Champion/broker sizing.
        risk["allow_fractional"] = True
        mutation_descriptor = ",".join(mutation_labels)
        raw = f"{parent.policy_id}|{parent.compute_fingerprint()}|{self.generation}|{mutation_descriptor}|{now.isoformat()}"
        child_id = f"spawn_g{self.generation}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        return ChildPolicyConfig(
            policy_id=child_id, parent_policy_id=parent.policy_id, policy_version=f"g{self.generation}", spawned_at=now,
            seed_capital=min(parent.seed_capital, self.settings.seed_capital), execution_mode="SHADOW_ONLY",
            strategy_overrides=strategy, ml_overrides=ml, meta_overrides=meta, risk_overrides=risk,
            code_commit_sha=parent.code_commit_sha, model_manifest_sha=parent.model_manifest_sha,
            spawn_reason=f"EVOLUTION_MULTI_MUTATION:{mutation_descriptor}",
        )

    @staticmethod
    def behavior_signature(executor: ChildPolicyExecutor) -> str:
        """Fingerprint observable completed trade behavior, independent of config identity."""
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
            ordered = sorted(ids, key=lambda policy_id: (score_by_id[policy_id].fitness, score_by_id[policy_id].trades, policy_id), reverse=True)
            weak.extend(ordered[1:])

        overflow = max(0, sum(executor.status == "ACTIVE" for executor in executors.values()) - self.settings.max_population)
        if overflow:
            weak.extend([item.policy_id for item in reversed(scored) if item.trades >= self.settings.min_trades_for_selection][:overflow])
        return list(dict.fromkeys(weak))