"""AlphaDesk 007 execution-path simplification.

Keeps the existing architecture but removes production-only dead weight and
fixes state/ordering bugs that can suppress valid trades.
"""
from __future__ import annotations

from functools import wraps
from typing import Any

PRODUCTION_STRATEGIES = {
    "momentum",
    "orb",
    "rsi2_reversion",
    "vwap_reversion",
    "trend_pullback",
}


def _install_strategy_whitelist() -> None:
    import app.strategies as strategies_pkg
    import app.strategies.base as strategies_base

    if getattr(strategies_pkg, "_alphadesk_007_whitelist", False):
        return
    original = strategies_base.create_strategies

    @wraps(original)
    def create_production_strategies(overrides: dict[str, dict] | None = None):
        overrides = overrides or {}
        filtered = {name: cfg for name, cfg in overrides.items() if name in PRODUCTION_STRATEGIES}
        # Explicitly create the known production set. Research/challenger
        # strategies remain registered and usable by research/backtest code.
        return [s for s in original(filtered) if s.name in PRODUCTION_STRATEGIES]

    strategies_base.create_strategies = create_production_strategies
    strategies_pkg.create_strategies = create_production_strategies
    strategies_pkg._alphadesk_007_whitelist = True


def _install_pipeline_authority() -> None:
    from app.strategies.pipeline import SignalPipeline

    if getattr(SignalPipeline, "_alphadesk_007_authority", False):
        return
    original_init = SignalPipeline.__init__

    @wraps(original_init)
    def forced_production_init(self, *args, **kwargs):
        args = list(args)
        if len(args) >= 2:
            args[1] = 0.50
        else:
            kwargs["ml_threshold"] = 0.50
        kwargs["soft_composite_gating"] = True
        original_init(self, *args, **kwargs)

    SignalPipeline.__init__ = forced_production_init
    SignalPipeline._alphadesk_007_authority = True


def _install_momentum_state_fix() -> None:
    from app.strategies.momentum import MomentumCross

    if getattr(MomentumCross, "_alphadesk_007_state_fix", False):
        return
    original_on_bar = MomentumCross.on_bar

    def no_global_regime_invalidation(self, _current_regime):
        # Regime state belongs to each symbol. The original implementation
        # cleared every symbol's pending MACD events whenever any symbol changed
        # regime, which could erase unrelated valid setups.
        return None

    @wraps(original_on_bar)
    def guarded_on_bar(self, ctx, symbol, df):
        symbol = symbol.upper()
        regimes = getattr(self, "_alphadesk_007_regimes", {})
        previous = regimes.get(symbol)
        if previous is not None and previous != ctx.regime:
            self._events.pop(symbol, None)
        regimes[symbol] = ctx.regime
        self._alphadesk_007_regimes = regimes

        result = original_on_bar(self, ctx, symbol, df)
        if result is not None:
            action = getattr(result, "action", None)
            direction = "long" if getattr(action, "name", "") == "ENTER_LONG" else "short" if getattr(action, "name", "") == "ENTER_SHORT" else None
            if direction:
                event = self._sym_events(symbol).get(direction)
                if event is not None:
                    event["emitted"] = True
        return result

    MomentumCross.invalidate_stale_events = no_global_regime_invalidation
    MomentumCross.on_bar = guarded_on_bar
    MomentumCross._alphadesk_007_state_fix = True


def _install_portfolio_ordering() -> None:
    import app.portfolio.allocator as allocator

    if getattr(allocator, "_alphadesk_007_ordering", False):
        return
    original = allocator.process_portfolio_allocations

    @wraps(original)
    def ranked_portfolio_decision(*args, **kwargs):
        # Rank first, then apply sector/correlation gates. This prevents a weak
        # earlier candidate from consuming the slot that belongs to the stronger
        # candidate.
        if args:
            signals = list(args[0])
            ranked = sorted(signals, key=lambda s: float(s.score or 0.0), reverse=True)
            args = (ranked,) + args[1:]
        elif "signals" in kwargs:
            kwargs["signals"] = sorted(
                list(kwargs["signals"]),
                key=lambda s: float(s.score or 0.0),
                reverse=True,
            )
        result = original(*args, **kwargs)
        # RiskManager is the sole live sizing authority. Kelly remains research
        # telemetry but never overrides the final production risk calculation.
        result.approved_entries = [(sig, None) for sig, _ in result.approved_entries]
        return result

    allocator.process_portfolio_allocations = ranked_portfolio_decision
    allocator._alphadesk_007_ordering = True


def _install_loss_cooldown_policy() -> None:
    from app.risk.manager import RiskManager

    if getattr(RiskManager, "_alphadesk_007_loss_policy", False):
        return
    original_close = RiskManager.on_trade_closed

    @wraps(original_close)
    def loss_only_cooldown(self, trade, now=None):
        result = original_close(self, trade, now=now)
        try:
            if float(trade.pnl) > 0:
                # A winning trade should not freeze the engine. Same-thesis and
                # catastrophic-loss protection remain active independently.
                self.global_cooldown_until = None
                self.cooldown_until = None
        except (TypeError, ValueError):
            pass
        return result

    RiskManager.on_trade_closed = loss_only_cooldown

    def no_strategy_wide_freeze(self, strategy: str, now=None) -> bool:
        # Strategy-wide loss streaks mix fundamentally different contexts and
        # were suppressing profitable momentum configurations. Same-thesis,
        # daily-loss and catastrophic-loss controls remain the real breakers.
        return False

    RiskManager.is_strategy_in_cooldown = no_strategy_wide_freeze
    RiskManager._alphadesk_007_loss_policy = True


def install() -> None:
    _install_strategy_whitelist()
    _install_pipeline_authority()
    _install_momentum_state_fix()
    _install_portfolio_ordering()
    _install_loss_cooldown_policy()
