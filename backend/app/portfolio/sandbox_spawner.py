"""CR-146E Sandbox Lifecycle Spawner Execution Plane.

Executes active CR-146E spawned challenger policies in isolated virtual sandboxes
with child-specific research capital, independent equity curves, virtual positions,
virtual fills, and P&L accounting. Zero connection to Champion broker orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.logging import applog
from app.portfolio.simulator import SimulationConfig, SimulationResult, VirtualPortfolioSimulator
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_executor import ChildPolicyExecutor


@dataclass
class SpawnedChildSandbox:
    policy_id: str
    spawned_at: datetime
    seed_capital: float
    status: str = "ACTIVE"
    simulator: VirtualPortfolioSimulator = field(init=False)
    virtual_signals: list[dict] = field(default_factory=list)
    virtual_trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    executor: ChildPolicyExecutor | None = field(default=None)

    def __post_init__(self):
        cfg = SimulationConfig(
            starting_capital=self.seed_capital,
            risk_per_trade_pct=0.5,
            max_position_pct=25.0,
            max_open_positions=2,
        )
        self.simulator = VirtualPortfolioSimulator(config=cfg)
        if self.executor is None:
            child_cfg = ChildPolicyConfig(
                policy_id=self.policy_id,
                spawned_at=self.spawned_at,
                seed_capital=self.seed_capital,
                status=self.status
            )
            self.executor = ChildPolicyExecutor(child_cfg)

    def is_eligible_at(self, ts: datetime) -> bool:
        """Enforce causal spawn boundary: child cannot trade before spawned_at."""
        if self.status != "ACTIVE":
            return False
        if not isinstance(self.spawned_at, datetime):
            return False
        if self.spawned_at.tzinfo is None and ts.tzinfo is not None:
            spawned_tz = self.spawned_at.replace(tzinfo=ts.tzinfo)
        elif self.spawned_at.tzinfo is not None and ts.tzinfo is None:
            spawned_tz = self.spawned_at.astimezone(ts.tzinfo)
        else:
            spawned_tz = self.spawned_at
        return ts >= spawned_tz

    def record_signal(self, signal: dict, ts: datetime) -> None:
        if self.is_eligible_at(ts):
            sig_copy = dict(signal)
            sig_copy["policy_id"] = self.policy_id
            if "timestamp" not in sig_copy:
                sig_copy["timestamp"] = ts
            self.virtual_signals.append(sig_copy)
            if self.executor:
                obs = {
                    "symbol": signal.get("symbol", "SPY"),
                    "strategy": signal.get("strategy", "momentum"),
                    "direction": signal.get("side", "long"),
                    "price": float(signal.get("price", 0.0)),
                    "ts": ts,
                    "champion_candidate": signal
                }
                self.executor.evaluate_observation(obs)

    def finalize_simulation(self) -> SimulationResult:
        if not self.virtual_signals:
            return SimulationResult(
                trades=[],
                equity_curve=[(self.spawned_at, self.seed_capital)],
                daily_returns=[],
                metrics={"total_return_pct": 0.0, "total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0},
            )
        res = self.simulator.run(self.virtual_signals)
        self.virtual_trades = res.trades
        self.equity_curve = res.equity_curve
        self.metrics = res.metrics
        return res


class SpawnedSandboxExecutor:
    """Manages concurrent active CR-146E child strategy sandboxes."""

    def __init__(self, spawned_rows: list[dict] | None = None) -> None:
        self.children: dict[str, SpawnedChildSandbox] = {}
        if spawned_rows:
            for row in spawned_rows:
                policy_id = row.get("policy_id")
                seed_cap = float(row.get("seed_capital", 10000.0))
                spawned_at_raw = row.get("spawned_at")
                status = row.get("status", "ACTIVE")
                
                # Enforce fail-closed for malformed timestamps
                if isinstance(spawned_at_raw, str):
                    try:
                        spawned_at = datetime.fromisoformat(spawned_at_raw)
                    except ValueError:
                        status = "UNAVAILABLE"
                        spawned_at = None
                elif isinstance(spawned_at_raw, datetime):
                    spawned_at = spawned_at_raw
                else:
                    status = "UNAVAILABLE"
                    spawned_at = None

                if policy_id and spawned_at is not None:
                    self.children[policy_id] = SpawnedChildSandbox(
                        policy_id=policy_id,
                        spawned_at=spawned_at,
                        seed_capital=seed_cap,
                        status=status,
                    )

    def process_signal_for_children(self, signal: dict, ts: datetime) -> None:
        """Route market signal to all eligible child sandboxes."""
        for child in self.children.values():
            if child.status == "ACTIVE" and child.is_eligible_at(ts):
                child.record_signal(signal, ts)

    def finalize_all(self) -> dict[str, dict]:
        """Finalize virtual simulation accounting for all active children."""
        results = {}
        for policy_id, child in self.children.items():
            res = child.finalize_simulation()
            results[policy_id] = {
                "policy_id": policy_id,
                "spawned_at": child.spawned_at.isoformat() if hasattr(child.spawned_at, "isoformat") else str(child.spawned_at),
                "seed_capital": child.seed_capital,
                "final_equity": res.equity_curve[-1][1] if res.equity_curve else child.seed_capital,
                "metrics": res.metrics,
                "total_trades": len(res.trades),
                "trades": res.trades,
            }
            applog.info(
                "sandbox_spawner",
                f"[ChildSandbox][{policy_id}] Finalized: trades={len(res.trades)} "
                f"return={res.metrics.get('total_return_pct', 0.0):.2f}%",
            )
        return results
