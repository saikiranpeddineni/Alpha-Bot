"""Strategy package -- importing it registers all built-in strategies."""

from app.strategies import bb_squeeze, momentum, orb, rsi_reversion, vwap_reversion  # noqa: F401
from app.strategies.base import REGISTRY, Strategy, StrategyContext, create_strategies  # noqa: F401
from app.strategies.pipeline import SignalPipeline  # noqa: F401
from app.strategies.evaluator import ParallelEvidenceEvaluator  # noqa: F401
