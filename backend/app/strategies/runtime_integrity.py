"""Production-path integrity guards for dynamic market intelligence.

The module installs small, idempotent compatibility boundaries at startup.
It keeps research-only adaptive values out of Champion ML/Meta execution,
ensures scanner context reaches strategy signals, and makes contradictory
entry context visible to the existing pipeline before expensive inference.
"""
from __future__ import annotations

from functools import wraps
from typing import Any


_CURRENT_MOVER_CONTEXT: dict[str, dict[str, Any]] = {}


def install_runtime_integrity() -> None:
    """Install idempotent production-path integrity fixes."""
    _install_champion_threshold_boundary()
    _install_dynamic_mover_context_bridge()
    _install_entry_context_boundary()


def get_dynamic_mover_context(symbol: str) -> dict[str, Any]:
    """Return the latest scanner-derived mover context for a symbol."""
    return dict(_CURRENT_MOVER_CONTEXT.get(symbol.upper(), {}))


def _install_champion_threshold_boundary() -> None:
    """Keep contextual/adaptive thresholds in research, not Champion execution."""
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
        # The Champion boundary intentionally ignores research-zone adaptive
        # thresholds. Existing configured runtime thresholds remain authoritative.
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
    """Bridge live scanner mover context into strategy-produced Signals."""
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

            signal = method(self, ctx, symbol, df)
            if signal is not None:
                # Typed Signal fields make this contract explicit rather than
                # relying on ad-hoc attributes or a second context source.
                signal.mover_bias = getattr(ctx, "mover_bias", None)
                signal.mover_change_pct = float(getattr(ctx, "mover_change_pct", 0.0) or 0.0)
                signal.rs_score = getattr(ctx, "rs_score", None)
            return signal

        on_bar_with_context._mover_bridge_installed = True
        strategy_cls.on_bar = on_bar_with_context

    _wrap_strategy(GapAndGo)
    _wrap_strategy(MomentumCross)


def _install_entry_context_boundary() -> None:
    """Apply evidence-based context consistency before Champion inference."""
    from app.strategies.pipeline import SignalPipeline
    from app.strategies.entry_intelligence import assess_entry_context

    if getattr(SignalPipeline.process, "_entry_context_boundary_installed", False):
        return

    original_process = SignalPipeline.process

    @wraps(original_process)
    def context_aware_process(
        self,
        symbol,
        df,
        signals,
        regime,
        features=None,
        sector_strength=None,
        adaptive_ml_thresholds=None,
    ):
        filtered = []
        context_rejections: list[tuple[Any, str]] = []
        for sig in signals:
            if getattr(sig, "action", None).value not in ("enter_long", "enter_short"):
                filtered.append(sig)
                continue

            assessment = assess_entry_context(sig, df, regime)
            sig.context_conflict_count = len(assessment.reasons)
            sig.context_conflict_reasons = list(assessment.reasons)
            if not assessment.allowed:
                reason = "Entry context conflict: " + "; ".join(assessment.reasons)
                context_rejections.append((sig, reason))
                continue
            filtered.append(sig)

        accepted, rejected = original_process(
            self,
            symbol,
            df,
            filtered,
            regime,
            features=features,
            sector_strength=sector_strength,
            adaptive_ml_thresholds=None,
        )
        return accepted, context_rejections + rejected

    # Preserve the marker from the Champion wrapper as well so repeated imports
    # cannot stack wrappers indefinitely.
    context_aware_process._entry_context_boundary_installed = True
    context_aware_process._champion_boundary_installed = True
    SignalPipeline.process = context_aware_process
