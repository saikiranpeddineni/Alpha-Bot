"""Production strategy package for AlphaDesk 007.

Only the five production strategies are registered here. Research strategies
and research gates are intentionally not imported during production startup.
"""

from app.strategies import momentum_simple, orb, rsi_reversion, vwap_reversion, trend_pullback  # noqa: F401
from app.strategies.base import REGISTRY, Strategy, StrategyContext, create_strategies  # noqa: F401
from app.strategies.pipeline import SignalPipeline  # noqa: F401


class ParallelEvidenceEvaluator:
    """Compatibility no-op. Research evidence cannot enter production."""
    def __init__(self, *args, **kwargs) -> None:
        pass

    def evaluate(self, *args, **kwargs):
        return None
