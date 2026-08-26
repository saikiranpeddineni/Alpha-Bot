"""Strategy framework.

A strategy is a pure decision function: given the enriched bar frame and a
small context (position, regime, equity), emit a Signal or None. Strategies
never size positions or talk to brokers -- the AI pipeline gates their
signals and the risk manager sizes them. The same instance code runs live
and in backtests.

Register new strategies with @register; they appear automatically in the
API, UI and backtester.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from app.core.domain import Position, Regime, Signal


@dataclass
class StrategyContext:
    position: Position | None
    equity: float
    regime: Regime
    now: datetime
    debug_mode: bool = False
    # Populated by scanner when a symbol is a known top gainer or loser.
    # Values: "gainer" (long bias) | "loser" (short bias) | None (neutral)
    mover_bias: str | None = None
    # Raw % change for the mover (positive for gainers, negative for losers)
    mover_change_pct: float = 0.0
    # Relative strength score vs SPY (0.0 to 1.0)
    rs_score: float | None = None
    # Multi-day anchor: SMA20 of daily closes (None when unavailable).
    # Enables drift filters -- e.g. ORB longs below the 20-day mean fight
    # persistent order flow (Betadesk's single biggest measured ORB loss source).
    daily_sma20: float | None = None


class Strategy(ABC):
    """Subclass and set: name, description, default_params; implement on_bar."""

    name: str = "abstract"
    description: str = ""
    default_params: dict = {}

    # --- Dynamic Core Traits ---
    max_allocation: float = 50000.0
    is_breakout: bool = False
    # Mean-reversion entries deliberately fade vertical moves (capitulation
    # bars, swept lows). Bar-shape vetoes tuned for momentum chasing must not
    # apply to them -- entering against the extreme IS the thesis.
    is_mean_reversion: bool = False
    ml_threshold: float = 0.53
    evaluation_horizons: list[str] = ["15m", "close"]
    regime_weights: dict[Regime, float] = {
        Regime.TREND_UP: 1.0, Regime.TREND_DOWN: 1.0, Regime.RANGE: 1.0, Regime.VOLATILE: 1.0
    }

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        self.params: dict = {**self.default_params, **(params or {})}
        self.enabled = enabled

    @abstractmethod
    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        """Called once per newly closed bar with the enriched frame (oldest->newest)."""

    def reset(self) -> None:
        """Clear per-session state (fresh backtest / engine restart)."""

    def describe(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "params": self.params,
        }


REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if cls.name in REGISTRY:
        raise ValueError(f"Duplicate strategy name {cls.name!r}")
    REGISTRY[cls.name] = cls
    return cls


def create_strategies(overrides: dict[str, dict] | None = None) -> list[Strategy]:
    """Instantiate every registered strategy, applying {name: {enabled, params}} overrides."""
    overrides = overrides or {}
    out: list[Strategy] = []
    for name, cls in REGISTRY.items():
        cfg = overrides.get(name, {})
        out.append(cls(params=cfg.get("params"), enabled=cfg.get("enabled", True)))
    return out
