"""Production-path compatibility bridges for the existing AlphaDesk architecture.

The canonical trading decisions remain in the existing engine, pipeline, risk,
and PIS modules. This module only preserves compatibility behavior that cannot
be expressed safely at the existing call sites without changing public APIs.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any

_CURRENT_MOVER_CONTEXT: dict[str, dict[str, Any]] = {}


def install_runtime_integrity() -> None:
    """Install idempotent compatibility bridges used by the live execution path."""
    _install_champion_threshold_boundary()
    _install_dynamic_mover_context_bridge()


def get_dynamic_mover_context(symbol: str) -> dict[str, Any]:
    return dict(_CURRENT_MOVER_CONTEXT.get(symbol.upper(), {}))


def _install_champion_threshold_boundary() -> None:
    from app.strategies.pipeline import SignalPipeline

    if getattr(SignalPipeline.process, "_champion_boundary_installed", False):
        return
    original_process = SignalPipeline.process

    @wraps(original_process)
    def champion_process(
        self,
        symbol,
        df,
        signals,
        regime,
        features=None,
        sector_strength=None,
        adaptive_ml_thresholds=None,
    ):
        # Champion execution owns the configured ML/Meta thresholds. Contextual
        # expectancy is still recorded/learned by the memory layer, but research
        # thresholds cannot silently rewrite the production policy.
        return original_process(
            self,
            symbol,
            df,
            signals,
            regime,
            features=features,
            sector_strength=sector_strength,
            adaptive_ml_thresholds=None,
        )

    champion_process._champion_boundary_installed = True
    SignalPipeline.process = champion_process


def _install_dynamic_mover_context_bridge() -> None:
    """Carry scanner mover evidence into legacy direct strategy callers.

    The normal engine path already supplies mover context through StrategyContext.
    This bridge is retained only for direct strategy callers that predate that
    canonical path; it does not create a second trading decision.
    """
    from app.core.scanner import MarketScanner
    from app.strategies.gap_and_go import GapAndGo
    from app.strategies.momentum import MomentumCross

    if not getattr(MarketScanner._stage1_symbols, "_mover_bridge_installed", False):
        original_stage1 = MarketScanner._stage1_symbols

        @wraps(original_stage1)
        async def stage1_with_context(self, *args, **kwargs):
            symbols = await original_stage1(self, *args, **kwargs)
            context = getattr(getattr(self, "_gap_scanner", None), "mover_context", {})
            global _CURRENT_MOVER_CONTEXT
            _CURRENT_MOVER_CONTEXT = {
                str(symbol).upper(): dict(value)
                for symbol, value in context.items()
                if isinstance(value, dict)
            }
            self.mover_context = dict(_CURRENT_MOVER_CONTEXT)
            return symbols

        stage1_with_context._mover_bridge_installed = True
        MarketScanner._stage1_symbols = stage1_with_context

    def _wrap_strategy(strategy_cls) -> None:
        method = strategy_cls.on_bar
        if getattr(method, "_mover_bridge_installed", False):
            return

        @wraps(method)
        def on_bar_with_context(self, ctx, symbol, df):
            entry = get_dynamic_mover_context(symbol)
            if entry:
                if getattr(ctx, "mover_bias", None) is None:
                    ctx.mover_bias = entry.get("direction")
                if not getattr(ctx, "mover_change_pct", 0.0):
                    try:
                        ctx.mover_change_pct = float(entry.get("change_pct", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        ctx.mover_change_pct = 0.0
            signal = method(self, ctx, symbol, df)
            if signal is not None:
                signal.mover_bias = getattr(ctx, "mover_bias", None)
                signal.mover_change_pct = float(getattr(ctx, "mover_change_pct", 0.0) or 0.0)
                signal.rs_score = getattr(ctx, "rs_score", None)
            return signal

        on_bar_with_context._mover_bridge_installed = True
        strategy_cls.on_bar = on_bar_with_context

    _wrap_strategy(GapAndGo)
    _wrap_strategy(MomentumCross)



