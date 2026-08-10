"""Production-path integrity guards for dynamic market intelligence.

This module keeps research-only adaptive values out of the Champion execution
path, restores the canonical market-risk field at the portfolio boundary, and
bridges live scanner mover context into the strategies that consume it.

It intentionally does not introduce new thresholds, widen existing thresholds,
or add another trading gate.
"""

from __future__ import annotations

from functools import wraps
from typing import Any


_CURRENT_MOVER_CONTEXT: dict[str, dict[str, Any]] = {}


def install_runtime_integrity() -> None:
    """Install idempotent production-path compatibility fixes."""
    _install_safety_decision_alias()
    _install_champion_threshold_boundary()
    _install_dynamic_mover_context_bridge()


def get_dynamic_mover_context(symbol: str) -> dict[str, Any]:
    """Return the latest scanner-derived mover context for a symbol."""
    return dict(_CURRENT_MOVER_CONTEXT.get(symbol.upper(), {}))


def _install_safety_decision_alias() -> None:
    from app.core.domain import SafetyDecision

    if not hasattr(SafetyDecision, "market_state"):
        # PortfolioAIGate already supports a dynamic market-state input. The
        # engine's canonical SafetyDecision field is market_risk_state; expose
        # that same value under the portfolio-gate compatibility name instead of
        # inventing a second state source.
        setattr(
            SafetyDecision,
            "market_state",
            property(lambda self: self.market_risk_state),
        )


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
        # Contextual/adaptive thresholds are research evidence. The Champion
        # pipeline uses its configured runtime threshold, which can still be
        # changed by the existing policy/configuration machinery. No threshold
        # is widened or lowered here.
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
    from app.core.scanner import MarketScanner
    from app.strategies.gap_and_go import GapAndGo
    from app.strategies.momentum import MomentumCross

    if not getattr(MarketScanner._stage1_symbols, "_mover_bridge_installed", False):
        original_stage1 = MarketScanner._stage1_symbols

        @wraps(original_stage1)
        async def stage1_with_context(self, *args, **kwargs):
            symbols = await original_stage1(self, *args, **kwargs)
            global _CURRENT_MOVER_CONTEXT
            context = getattr(getattr(self, "_gap_scanner", None), "mover_context", {})
            _CURRENT_MOVER_CONTEXT = {
                str(symbol).upper(): dict(value)
                for symbol, value in context.items()
                if isinstance(value, dict)
            }
            # Expose the same live context on the scanner instance for any
            # downstream consumer that has a direct scanner reference.
            self.mover_context = dict(_CURRENT_MOVER_CONTEXT)
            return symbols

        stage1_with_context._mover_bridge_installed = True
        MarketScanner._stage1_symbols = stage1_with_context

    def _wrap_strategy(strategy_cls):
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
            return method(self, ctx, symbol, df)

        on_bar_with_context._mover_bridge_installed = True
        strategy_cls.on_bar = on_bar_with_context

    _wrap_strategy(GapAndGo)
    _wrap_strategy(MomentumCross)
