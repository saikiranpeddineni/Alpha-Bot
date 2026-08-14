"""Minimal strategy framework for AlphaDesk 007."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from app.core.domain import Position, Regime, Signal
from app.core.applog import applog


@dataclass
class StrategyContext:
    position: Position | None
    equity: float
    regime: Regime
    now: datetime
    debug_mode: bool = False
    mover_bias: str | None = None
    mover_change_pct: float = 0.0
    rs_score: float | None = None
    news_direction: str = "neutral"
    news_confidence: float = 0.0
    event_state: str | None = None


class Strategy(ABC):
    name: str = "abstract"
    description: str = ""
    default_params: dict = {}
    max_allocation: float = 50000.0
    is_breakout: bool = False
    ml_threshold: float = 0.50
    evaluation_horizons: list[str] = ["15m", "close"]
    regime_weights: dict[Regime, float] = {
        Regime.TREND_UP: 1.0,
        Regime.TREND_DOWN: 1.0,
        Regime.RANGE: 1.0,
        Regime.VOLATILE: 1.0,
    }

    def __init__(self, params: dict | None = None, enabled: bool = True) -> None:
        self.params = {**self.default_params, **(params or {})}
        self.enabled = enabled

    @abstractmethod
    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        """Emit one entry signal or None."""

    def signal_accepted(self, sig: Signal) -> None:
        """Called after the broker accepts the entry."""

    def reset(self) -> None:
        """Reset strategy-local state."""

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description, "enabled": self.enabled, "params": self.params}


REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if cls.name in REGISTRY:
        raise ValueError(f"Duplicate strategy name {cls.name!r}")
    REGISTRY[cls.name] = cls
    return cls


# These are the only strategies allowed to generate production entries.
PRODUCTION_STRATEGIES = {
    "momentum",
    "orb",
    "rsi2_reversion",
    "vwap_reversion",
    "trend_pullback",
}


def create_strategies(overrides: dict[str, dict] | None = None) -> list[Strategy]:
    """Create exactly the 007 production strategies.

    Research strategies may exist elsewhere in the repository, but they are
    not imported/registered by the production strategy package and therefore
    cannot accidentally trade.
    """
    overrides = overrides or {}
    out: list[Strategy] = []
    for name, cls in REGISTRY.items():
        if name not in PRODUCTION_STRATEGIES:
            continue
        cfg = overrides.get(name, {}) or {}
        params = cfg.get("params") if isinstance(cfg.get("params"), dict) else None
        enabled = bool(cfg.get("enabled", True))
        out.append(cls(params=params, enabled=enabled))
    enabled_names = [s.name for s in out if s.enabled]
    applog.info("strategy", f"007 production strategies: {enabled_names}")
    return out
