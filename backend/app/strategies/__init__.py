"""Built-in strategies and small compatibility exports.

Production entry execution uses only the strategy classes and the compact
EntryDecision path. Research modules remain importable directly but are not
installed as runtime gates or monkey patches.
"""

from app.strategies import bb_squeeze, momentum, orb, rsi_reversion, vwap_reversion, gap_and_go, trend_pullback, liquidity_sweep, relative_strength  # noqa: F401
from app.strategies.base import REGISTRY, Strategy, StrategyContext, create_strategies  # noqa: F401
from app.strategies.pipeline import SignalPipeline  # noqa: F401

# Research compatibility exports. These are not called by the production
# decision path.
from app.strategies.memory import get_contextual_expectancy, refresh_contextual_memory, get_episodic_nearest_neighbors  # noqa: F401
from app.profit_dynamics import adaptive_ml_threshold, adaptive_signal_score
get_adaptive_ml_threshold = adaptive_ml_threshold
get_adaptive_signal_score = adaptive_signal_score

from app.strategies.reflection import generate_eod_reflection_report  # noqa: F401
