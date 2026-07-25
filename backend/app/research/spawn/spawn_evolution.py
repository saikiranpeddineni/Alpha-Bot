"""Bounded evolutionary controller for the SHADOW-ONLY spawn laboratory.

Objective: maximize repeatable edge extracted from scarce capital. Gross exposure is not a
fitness objective. Children are rewarded for expectancy, risk efficiency, capital velocity,
and profit per peak dollar, with explicit drawdown/capital-intensity penalties.
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
from app.research.spawn.spawn_capital_intelligence import analyze_closed_trades, velocity_score


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
    capital_intensity_penalty: float = 0.35
    drawdown_linear_penalty: float = 0.15
    drawdown_quadratic_penalty: float = 0.02
    edge_density_weight: float = 1.25
    capital_velocity_weight: float = 0.06
    capital_time_weight: float = 0.08


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
    risk_efficiency: float = 0.0
    capital_turnover: float = 0.0
    capital_minutes: float = 0.0
    pnl_per_capital_minute: float = 0.0
    compounding_state: str = "BASE"


class SpawnEvolutionEngine:
    PARAM_BOUNDS = {
        ("ml", "ml_threshold"): (0.48, 0.80),
        ("risk", "min_trade_score"): (20.0, 95.0),
        ("risk", "risk_per_trade_pct"): (0.10, 2.0),
        ("risk", "max_position_pct"): (5.0, 45.0),
        ("risk", "trailing_stop_atr_mult"): (0.0, 4.0),
        ("risk", "profit_reinvestment_fraction"): (0.0, 0.75),
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
        peak = float(getattr(portfolio, "peak_capital_deployed", 0.0) or 0.0)
        return (peak / portfolio.seed_capital * 100.0) if portfolio.seed_capital > 0 else 0.0

    def score(self, executor: ChildPolicyExecutor) -> FitnessSnapshot:
        p = executor.portfolio
        trades = int(p.trade_count)
        realized_r = float(p.realized_r)
        expectancy_r = realized_r / trades if trades else 0.0
        drawdown_pct = float(p.get_max_drawdown_pct())
        equity_return_pct = ((p.marked_equity / p.seed_capital) - 1.0) * 100.0 if p.seed_capital else 0.0
        peak_capital_pct = self._peak_capital_pct(p)
        metrics = p.capital_efficiency_metrics() if hasattr(p, "capital_efficiency_metrics") else {}
        capital_efficiency = float(metrics.get("return_per_peak_capital_pct", equity_return_pct / max(peak_capital_pct, 1.0)))
        risk_efficiency = float(metrics.get("risk_efficiency", 0.0))
        capital_turnover = float(metrics.get("capital_turnover", 0.0))
        intelligence = analyze_closed_trades(p.closed_trades, min_evidence_trades=max(5, self.settings.min_trades_for_selection // 2))
        eligible = executor.status == "ACTIVE" and trades >= self.settings.min_trades_for_selection and drawdown_pct <= self.settings.max_drawdown_pct

        confidence = min(1.0, trades / float(self.settings.confidence_trade_count)) if trades else 0.0
        growth_fraction = max(-0.999999, equity_return_pct / 100.0)
        growth_score = math.log1p(growth_fraction) * 10.0
        evidence_adjusted_edge = confidence * expectancy_r
        efficiency_score = confidence * capital_efficiency * 4.0
        edge_density_score = confidence * max(-5.0, min(5.0, risk_efficiency)) * self.settings.edge_density_weight
        turnover_score = confidence * math.log1p(max(0.0, capital_turnover)) * self.settings.capital_velocity_weight if expectancy_r > 0 else 0.0
        capital_time_score = velocity_score(intelligence) * self.settings.capital_time_weight if expectancy_r > 0 else 0.0
        drawdown_penalty = self.settings.drawdown_linear_penalty * drawdown_pct + self.settings.drawdown_quadratic_penalty * drawdown_pct * drawdown_pct
        capital_penalty = self.settings.capital_intensity_penalty * (peak_capital_pct / 100.0)
        fitness = evidence_adjusted_edge + growth_score + efficiency_score + edge_density_score + turnover_score + capital_time_score - drawdown_penalty - capital_penalty
        if not eligible:
            fitness = -1_000_000.0 + fitness
        return FitnessSnapshot(executor.policy_id, trades, realized_r, expectancy_r, drawdown_pct, equity_return_pct, fitness, eligible, peak_capital_pct, capital_efficiency, risk_efficiency, capital_turnover, intelligence.capital_minutes, intelligence.pnl_per_capital_minute, intelligence.state)

    def rank(self, executors: Iterable[ChildPolicyExecutor]) -> list[FitnessSnapshot]:
        return sorted((self.score(e) for e in executors), key=lambda x: (x.fitness, x.capital_efficiency, x.risk_efficiency, x.expectancy_r, x.trades, x.policy_id), reverse=True)

    def select_elites(self, executors: dict[str, ChildPolicyExecutor]) -> list[ChildPolicyExecutor]:
        return [executors[x.policy_id] for x in self.rank(executors.values()) if x.eligible][: self.settings.elite_count]

    def bootstrap_configs(self, now: datetime | None = None) -> list[ChildPolicyConfig]:
        now = now or datetime.now(timezone.utc)
        seeds = [
            (0.48, 45.0, 0.35, 0.0, 8.0, 0.00),
            (0.50, 50.0, 0.50, 0.5, 12.0, 0.15),
            (0.52, 55.0, 0.65, 1.0, 16.0, 0.30),
            (0.55, 60.0, 0.80, 1.5, 20.0, 0.45),
            (0.58, 65.0, 0.60, 2.0, 30.0, 0.60),
            (0.62, 70.0, 0.45, 2.5, 40.0, 0.75),
        ]
        out = []
        for i, (ml, score, risk, trail, cap, reinvest) in enumerate(seeds[: self.settings.bootstrap_population]):
            out.append(ChildPolicyConfig(policy_id=f"spawn_g0_seed_{i+1:02d}", parent_policy_id="champion", policy_version="g0", spawned_at=now, seed_capital=self.settings.seed_capital, execution_mode="SHADOW_ONLY", ml_overrides={"ml_threshold": ml}, risk_overrides={"min_trade_score": score, "risk_per_trade_pct": risk, "max_position_pct": cap, "trailing_stop_atr_mult": trail, "profit_reinvestment_fraction": reinvest, "allow_fractional": True}, spawn_reason="BOOTSTRAP_EDGE_DENSITY_VELOCITY_COMPOUNDING"))
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
        defaults = {"ml_threshold": 0.48, "min_trade_score": 50.0, "risk_per_trade_pct": 0.5, "max_position_pct": 15.0, "trailing_stop_atr_mult": 0.0, "profit_reinvestment_fraction": 0.0}
        dimensions = list(self.PARAM_BOUNDS)
        selected = self.rng.sample(dimensions, k=2 if self.rng.random() < 0.70 else 1)
        labels = []
        for group, key in selected:
            target = {"ml": ml, "risk": risk}[group]
            lo, hi = self.PARAM_BOUNDS[(group, key)]
            target[key] = round(self._mutate_value(target.get(key, defaults[key]), lo, hi), 6)
            labels.append(f"{group}.{key}={target[key]}")
        risk["allow_fractional"] = True
        descriptor = ",".join(labels)
        raw = f"{parent.policy_id}|{parent.compute_fingerprint()}|{self.generation}|{descriptor}|{now.isoformat()}"
        child_id = f"spawn_g{self.generation}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        return ChildPolicyConfig(policy_id=child_id, parent_policy_id=parent.policy_id, policy_version=f"g{self.generation}", spawned_at=now, seed_capital=min(parent.seed_capital, self.settings.seed_capital), execution_mode="SHADOW_ONLY", strategy_overrides=strategy, ml_overrides=ml, meta_overrides=meta, risk_overrides=risk, code_commit_sha=parent.code_commit_sha, model_manifest_sha=parent.model_manifest_sha, spawn_reason=f"EVOLUTION_EDGE_DENSITY_VELOCITY:{descriptor}")

    @staticmethod
    def behavior_signature(executor: ChildPolicyExecutor) -> str:
        payload = "|".join(f"{t.get('symbol')}:{t.get('side')}:{t.get('entry_at')}:{t.get('exit_at')}:{t.get('exit_reason')}" for t in executor.portfolio.closed_trades)
        return hashlib.sha256(payload.encode()).hexdigest()

    def next_generation(self, executors, now=None):
        elites = self.select_elites(executors)
        if not elites: return []
        count = min(max(0, self.settings.max_population - sum(e.status == "ACTIVE" for e in executors.values())), self.settings.children_per_cycle)
        if not count: return []
        self.generation += 1
        out, seen, attempts = [], {e.config_fingerprint for e in executors.values()}, 0
        while len(out) < count and attempts < count * 30:
            parent = elites[attempts % len(elites)]
            attempts += 1
            child = self.mutate(parent.config, now)
            fp = child.compute_fingerprint()
            if fp not in seen:
                seen.add(fp); out.append(child)
        return out

    def behavior_clone_groups(self, executors: dict[str, ChildPolicyExecutor], min_trades: int = 1) -> dict[str, list[str]]:
        groups = {}
        for e in executors.values():
            if e.portfolio.trade_count >= min_trades:
                groups.setdefault(self.behavior_signature(e), []).append(e.policy_id)
        return {sig: ids for sig, ids in groups.items() if len(ids) > 1}

    def retirement_candidates(self, executors):
        scored = self.rank(executors.values())
        sampled = [x for x in scored if x.trades >= self.settings.min_trades_for_selection]
        weak = [x.policy_id for x in sampled if x.drawdown_pct > self.settings.max_drawdown_pct or x.expectancy_r < -0.20 or x.risk_efficiency < -0.25]
        score_by_id = {x.policy_id: x for x in scored}
        for ids in self.behavior_clone_groups(executors, self.settings.min_trades_for_selection).values():
            ordered = sorted(ids, key=lambda pid: (score_by_id[pid].fitness, score_by_id[pid].capital_efficiency, score_by_id[pid].risk_efficiency, score_by_id[pid].pnl_per_capital_minute, score_by_id[pid].trades, pid), reverse=True)
            weak.extend(ordered[1:])
        overflow = max(0, sum(e.status == "ACTIVE" for e in executors.values()) - self.settings.max_population)
        if overflow:
            weak.extend([x.policy_id for x in reversed(scored) if x.trades >= self.settings.min_trades_for_selection][:overflow])
        return list(dict.fromkeys(weak))