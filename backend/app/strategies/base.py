"""Strategy framework.

A strategy is a pure decision function: given the enriched bar frame and a
small context (position, regime, equity), emit a Signal or None. Strategies
never size positions or talk to brokers -- the AI pipeline gates their
signals and the risk manager sizes them. The same instance code runs live
and in backtests.
"""

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
        """Called once per newly closed bar with the enriched frame."""

    def signal_accepted(self, sig: Signal) -> None:
        pass

    def reset(self) -> None:
        pass

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


# Production 007 deliberately runs a compact set of strategies with evidence
# of useful behavior in the current DB. Experimental/challenger strategies stay
# registered for research/backtests but do not create live entries by default.
PRODUCTION_STRATEGIES = {
    "rsi2_reversion",
    "orb",
    "vwap_reversion",
    "trend_pullback",
}


def create_strategies(overrides: dict[str, dict] | None = None) -> list[Strategy]:
    """Instantiate only the compact AlphaDesk 007 production strategy set."""
    overrides = overrides or {}
    out: list[Strategy] = []
    for name, cls in REGISTRY.items():
        cfg = overrides.get(name, {})
        explicitly_enabled = bool(cfg.get("enabled", False)) if name in overrides else True
        enabled = name in PRODUCTION_STRATEGIES and explicitly_enabled
        if name not in PRODUCTION_STRATEGIES and explicitly_enabled:
            applog.info("strategy", f"AlphaDesk 007: research strategy {name} kept registered but disabled for production")
        out.append(cls(params=cfg.get("params"), enabled=enabled))
    return out
