"""Strategy package -- importing it registers all built-in strategies."""

from app.strategies import bb_squeeze, momentum, orb, rsi_reversion, vwap_reversion, gap_and_go, trend_pullback, liquidity_sweep, relative_strength  # noqa: F401
from app.strategies.base import REGISTRY, Strategy, StrategyContext, create_strategies  # noqa: F401
from app.strategies.pipeline import SignalPipeline  # noqa: F401
from app.strategies.evaluator import ParallelEvidenceEvaluator  # noqa: F401
from app.strategies.memory import get_contextual_expectancy, refresh_contextual_memory, get_episodic_nearest_neighbors  # noqa: F401
from app.profit_dynamics import adaptive_ml_threshold, adaptive_signal_score

# Public compatibility names used by the engine. The dynamic controller keeps
# contextual adaptation evidence-driven while leaving the champion score immutable.
get_adaptive_ml_threshold = adaptive_ml_threshold
get_adaptive_signal_score = adaptive_signal_score

from app.strategies.reflection import generate_eod_reflection_report  # noqa: F401

# Install only execution-boundary integrity fixes. Research/adaptive evidence
# remains observable to the research evaluator but cannot silently change the
# Champion ML/Meta thresholds.
from app.strategies.runtime_integrity import install_runtime_integrity
install_runtime_integrity()
