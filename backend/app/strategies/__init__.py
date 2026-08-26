"""Strategy package -- importing it registers all built-in strategies."""

from app.strategies import (  # noqa: F401
    bb_squeeze,
    gap_and_go,
    liquidity_sweep,
    momentum,
    orb,
    relative_strength,
    rsi_reversion,
    trend_pullback,
    vwap_reversion,
)
from app.strategies.base import REGISTRY, Strategy, StrategyContext, create_strategies  # noqa: F401
from app.strategies.pipeline import SignalPipeline  # noqa: F401
