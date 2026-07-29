from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .exit_planner import PISExitPlanner
from .models import LifecycleState, PISDecision, PositionObservation, PositionSnapshot
from .state_machine import next_lifecycle_state
from .thesis_drift import classify_thesis_drift
from .trade_health import TradeHealthScorer


@dataclass
class PositionBrain:
    symbol: str
    side: str
    entry_price: float
    initial_stop: float
    entry_time: datetime
    strategy: str = "unknown"
    scorer: TradeHealthScorer = field(default_factory=TradeHealthScorer)
    planner: PISExitPlanner = field(default_factory=PISExitPlanner)
    lifecycle: LifecycleState = LifecycleState.NEW
    entry_health: float | None = None
    peak_r: float = 0.0
    trough_r: float = 0.0
    last_snapshot: PositionSnapshot | None = None
    history: list[PositionSnapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper()
        self.side = self.side.lower()
        if self.side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")
        self._risk_per_share = abs(float(self.entry_price) - float(self.initial_stop))
        if self._risk_per_share <= 1e-9:
            raise ValueError("initial_stop must define positive initial risk")

    def r_multiple(self, price: float) -> float:
        move = float(price) - float(self.entry_price)
        if self.side == "short":
            move = -move
        return move / self._risk_per_share

    def observe(self, observation: PositionObservation) -> PISDecision:
        current_r = self.r_multiple(observation.price)
        self.peak_r = max(self.peak_r, current_r)
        self.trough_r = min(self.trough_r, current_r)

        health_result = self.scorer.score(observation)
        if self.entry_health is None and health_result.score is not None:
            self.entry_health = health_result.score
        drift, thesis_state = classify_thesis_drift(self.entry_health, health_result.score)
        self.lifecycle = next_lifecycle_state(
            self.lifecycle,
            current_r=current_r,
            peak_r=self.peak_r,
            health=health_result.score,
            thesis_state=thesis_state,
        )
        snapshot = PositionSnapshot(
            timestamp=observation.timestamp,
            symbol=self.symbol,
            side=self.side,
            lifecycle=self.lifecycle,
            current_r=round(current_r, 4),
            peak_r=round(self.peak_r, 4),
            trough_r=round(self.trough_r, 4),
            giveback_r=round(max(0.0, self.peak_r - current_r), 4),
            health=health_result.score,
            entry_health=self.entry_health,
            thesis_drift=drift,
            thesis_state=thesis_state,
            components=dict(health_result.components),
        )
        self.last_snapshot = snapshot
        self.history.append(snapshot)
        return self.planner.decide(snapshot)

    def close(self) -> None:
        self.lifecycle = LifecycleState.CLOSED
