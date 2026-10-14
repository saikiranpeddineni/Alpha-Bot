"""Strategy package -- importing it registers all built-in strategies."""

# =============================================================================
# [MODIFIED 2026-10-14]: CR-02b - Register PullbackRearm Strategy
# ORIGINAL INTENT: Registered 9 built-in strategies.
# REASON FOR CHANGE: Register pullback_rearm second-chance continuation strategy.
# SAFE ROLLBACK: The original code is preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
# from app.strategies import (  # noqa: F401
#     bb_squeeze,
#     gap_and_go,
#     liquidity_sweep,
#     momentum,
#     orb,
#     relative_strength,
#     rsi_reversion,
#     trend_pullback,
#     vwap_reversion,
# )
# -----------------------------------------------------------------------------
# [NEW CODE]:
from app.strategies import (  # noqa: F401
    bb_squeeze,
    gap_and_go,
    liquidity_sweep,
    momentum,
    orb,
    pullback_rearm,
    relative_strength,
    rsi_reversion,
    trend_pullback,
    vwap_reversion,
)
# =============================================================================
from app.strategies.base import REGISTRY, Strategy, StrategyContext, create_strategies  # noqa: F401
from app.strategies.pipeline import SignalPipeline  # noqa: F401
