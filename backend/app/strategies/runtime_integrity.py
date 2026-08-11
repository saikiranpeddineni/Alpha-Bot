"""Production-path integrity guards for dynamic market intelligence."""
from __future__ import annotations

from functools import wraps
from typing import Any

_CURRENT_MOVER_CONTEXT: dict[str, dict[str, Any]] = {}
_LAST_LOSS_CONTEXT: dict[str, dict[str, Any]] = {}


def install_runtime_integrity() -> None:
    """Install idempotent production-path integrity fixes."""
    _install_champion_threshold_boundary()
    _install_dynamic_mover_context_bridge()
    _install_entry_context_boundary()
    _install_thesis_continuity_boundary()
    _install_engine_entry_boundary()


def get_dynamic_mover_context(symbol: str) -> dict[str, Any]:
    return dict(_CURRENT_MOVER_CONTEXT.get(symbol.upper(), {}))


def _install_champion_threshold_boundary() -> None:
    from app.strategies.pipeline import SignalPipeline
    if getattr(SignalPipeline.process, "_champion_boundary_installed", False):
        return
    original_process = SignalPipeline.process

    @wraps(original_process)
    def champion_process(self, symbol, df, signals, regime, features=None, sector_strength=None, adaptive_ml_thresholds=None):
        return original_process(
            self, symbol, df, signals, regime,
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
                signal.mover_bias = getattr(ctx, "mover_bias", None)
                signal.mover_change_pct = float(getattr(ctx, "mover_change_pct", 0.0) or 0.0)
                signal.rs_score = getattr(ctx, "rs_score", None)
            return signal

        on_bar_with_context._mover_bridge_installed = True
        strategy_cls.on_bar = on_bar_with_context

    _wrap_strategy(GapAndGo)
    _wrap_strategy(MomentumCross)


def _install_entry_context_boundary() -> None:
    from app.strategies.pipeline import SignalPipeline
    from app.strategies.entry_intelligence import assess_entry_context
    if getattr(SignalPipeline.process, "_entry_context_boundary_installed", False):
        return
    original_process = SignalPipeline.process

    @wraps(original_process)
    def context_aware_process(self, symbol, df, signals, regime, features=None, sector_strength=None, adaptive_ml_thresholds=None):
        filtered = []
        context_rejections: list[tuple[Any, str]] = []
        for sig in signals:
            if sig.action.value not in ("enter_long", "enter_short"):
                filtered.append(sig)
                continue
            assessment = assess_entry_context(sig, df, regime)
            sig.context_conflict_count = len(assessment.reasons)
            sig.context_conflict_reasons = list(assessment.reasons)
            if not assessment.allowed:
                context_rejections.append((sig, "Entry context conflict: " + "; ".join(assessment.reasons)))
            else:
                filtered.append(sig)

        accepted, rejected = original_process(
            self, symbol, df, filtered, regime,
            features=features,
            sector_strength=sector_strength,
            adaptive_ml_thresholds=None,
        )
        return accepted, context_rejections + rejected

    context_aware_process._entry_context_boundary_installed = True
    context_aware_process._champion_boundary_installed = True
    SignalPipeline.process = context_aware_process


def _install_thesis_continuity_boundary() -> None:
    """Block repeated same-thesis entries after a loss, without a timer."""
    from app.risk.manager import RiskManager
    if getattr(RiskManager.on_trade_closed, "_thesis_boundary_installed", False):
        return

    original_close = RiskManager.on_trade_closed
    original_permit = RiskManager.is_entry_permitted
    original_reconstruct = RiskManager.reconstruct_cooldowns

    def _context_from_trade(trade) -> dict[str, Any]:
        metadata = getattr(trade, "metadata", None) or {}
        return {
            "strategy": getattr(trade, "strategy", None),
            "side": getattr(trade, "side", None),
            "regime": metadata.get("regime") or metadata.get("market_regime"),
            "mover_bias": metadata.get("mover_bias"),
            "exit_at": getattr(trade, "exit_at", None),
        }

    @wraps(original_close)
    def on_trade_closed_with_context(self, trade, now=None):
        result = original_close(self, trade, now)
        if getattr(trade, "pnl", 0.0) <= 0:
            _LAST_LOSS_CONTEXT[str(trade.symbol).upper()] = _context_from_trade(trade)
        else:
            _LAST_LOSS_CONTEXT.pop(str(trade.symbol).upper(), None)
        return result

    @wraps(original_reconstruct)
    def reconstruct_with_context(self, trades, now=None):
        result = original_reconstruct(self, trades, now)
        for trade in trades:
            if getattr(trade, "pnl", 0.0) <= 0:
                _LAST_LOSS_CONTEXT[str(trade.symbol).upper()] = _context_from_trade(trade)
        return result

    @wraps(original_permit)
    def permit_with_context(self, signal, now=None):
        decision = original_permit(self, signal, now)
        if not decision.approved:
            return decision
        previous = _LAST_LOSS_CONTEXT.get(signal.symbol.upper())
        if not previous:
            return decision

        same_side = previous.get("side") == signal.side
        current_regime = getattr(signal.regime, "value", str(signal.regime) if signal.regime is not None else None)
        previous_regime = previous.get("regime")
        same_regime = bool(previous_regime and current_regime and str(previous_regime) == str(current_regime))
        previous_mover = previous.get("mover_bias")
        mover_changed = bool(previous_mover and signal.mover_bias and previous_mover != signal.mover_bias)

        if same_side and same_regime and not mover_changed:
            signal.thesis_continuity_blocked = True
            return type(decision)(False, "same-thesis re-entry blocked after prior loss; direction/regime context unchanged")
        return decision

    on_trade_closed_with_context._thesis_boundary_installed = True
    RiskManager.on_trade_closed = on_trade_closed_with_context
    RiskManager.reconstruct_cooldowns = reconstruct_with_context
    RiskManager.is_entry_permitted = permit_with_context


def _install_engine_entry_boundary() -> None:
    """Apply durable thesis/news checks to the final candidate boundary."""
    try:
        from app.core.engine_evaluation import TradingEngineEvaluationMixin
    except Exception:
        # The engine module is still being imported. main.py calls this installer
        # again after the engine class is fully loaded.
        return

    method = TradingEngineEvaluationMixin._evaluate_symbol
    if getattr(method, "_entry_boundary_installed", False):
        return

    from app.strategies.entry_intelligence import assess_thesis_continuity

    @wraps(method)
    async def evaluate_with_entry_intelligence(self, symbol, position, equity, now, effective_positions=None):
        candidates = await method(self, symbol, position, equity, now, effective_positions)
        if not candidates:
            return candidates

        out = []
        for sig, df in candidates:
            # Re-check failed-thesis continuity at the final execution boundary
            # using durable shadow evidence. No fixed elapsed-time cooldown.
            thesis = assess_thesis_continuity(getattr(self, "db", None), sig, now)
            if not thesis.allowed:
                sig.thesis_continuity_blocked = True
                from app.core.applog import applog
                applog.info("engine.entry", f"{sig.symbol}: rejected at final entry boundary: {'; '.join(thesis.reasons)}")
                continue

            # News is already an input to PIS. For entries, use the same provider
            # when available so an adverse catalyst cannot be invisible until exit.
            news_sentiment = 0.0
            try:
                data = getattr(self, "data", None)
                if data is not None and hasattr(data, "get_news_sentiment"):
                    news_sentiment = float(await data.get_news_sentiment(sig.symbol))
            except Exception:
                news_sentiment = 0.0
            sig.news_sentiment = news_sentiment

            if sig.side == "long" and news_sentiment <= -0.5:
                applog.info("engine.entry", f"{sig.symbol}: rejected adverse news context for LONG ({news_sentiment:.3f})")
                continue
            if sig.side == "short" and news_sentiment >= 0.5:
                applog.info("engine.entry", f"{sig.symbol}: rejected adverse news context for SHORT ({news_sentiment:.3f})")
                continue
            out.append((sig, df))
        return out

    evaluate_with_entry_intelligence._entry_boundary_installed = True
    TradingEngineEvaluationMixin._evaluate_symbol = evaluate_with_entry_intelligence
