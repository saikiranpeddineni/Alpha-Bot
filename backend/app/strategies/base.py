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
from dataclasses import dataclass, field
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


class Strategy(ABC):
    """Subclass and set: name, description, default_params; implement on_bar."""

    name: str = "abstract"
    description: str = ""
    default_params: dict = {}

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
        default_enabled = False if name == "rsi2_reversion" else True
        out.append(cls(params=cfg.get("params"), enabled=cfg.get("enabled", default_enabled)))
    return out
