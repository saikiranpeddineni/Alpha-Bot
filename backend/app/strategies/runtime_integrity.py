"""Production-path integrity bridges for the existing AlphaDesk architecture.

This module is intentionally a compatibility layer, not a second trading
architecture. The canonical entry-context decision lives in
``engine_evaluation.py`` and PIS remains the canonical discretionary exit path.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any

_CURRENT_MOVER_CONTEXT: dict[str, dict[str, Any]] = {}


def install_runtime_integrity() -> None:
    """Install idempotent compatibility bridges used by the live execution path."""
    _install_champion_threshold_boundary()
    _install_dynamic_mover_context_bridge()
    _install_risk_state_persistence()
    _install_event_calendar_news_bridge()


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
    """Carry the scanner's live mover evidence into the existing strategies."""
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


def _rebuild_strategy_cooldown_state(risk_manager, trades, now: datetime) -> None:
    """Reconstruct active strategy loss state from durable trade history."""
    cfg = risk_manager.config
    by_strategy: dict[str, list[Any]] = {}
    for trade in trades:
        if getattr(trade, "exit_at", None) is None:
            continue
        strategy = getattr(trade, "strategy", None) or "manual"
        by_strategy.setdefault(strategy, []).append(trade)

    for strategy, history in by_strategy.items():
        history.sort(key=lambda t: getattr(t, "exit_at"), reverse=True)
        streak = 0
        latest_loss_at = None
        for trade in history:
            if float(getattr(trade, "pnl", 0.0) or 0.0) <= 0:
                streak += 1
                if latest_loss_at is None:
                    latest_loss_at = getattr(trade, "exit_at")
            else:
                break
        if streak:
            risk_manager.strategy_consecutive_losses[strategy] = streak
            if (
                streak >= cfg.strategy_consecutive_loss_limit
                and latest_loss_at is not None
                and cfg.strategy_cooldown_minutes > 0
            ):
                cooldown_end = latest_loss_at + timedelta(minutes=cfg.strategy_cooldown_minutes)
                if cooldown_end > now:
                    risk_manager.strategy_cooldowns[strategy] = cooldown_end

    risk_manager.consecutive_losses = max(risk_manager.strategy_consecutive_losses.values(), default=0)
    active = [
        ts
        for ts in list(risk_manager.strategy_cooldowns.values()) + list(risk_manager.symbol_cooldowns.values())
        if ts > now
    ]
    risk_manager.cooldown_until = max(active) if active else None


def _install_risk_state_persistence() -> None:
    """Preserve valid restored cooldowns without creating a new risk policy."""
    from app.risk.manager import RiskManager

    if getattr(RiskManager.reconstruct_cooldowns, "_runtime_state_installed", False):
        return

    original_reconstruct = RiskManager.reconstruct_cooldowns
    original_roll = RiskManager.roll_session

    @wraps(original_reconstruct)
    def reconstruct_with_state(self, trades, now=None):
        result = original_reconstruct(self, trades, now)
        now_dt = now or datetime.now(UTC)
        _rebuild_strategy_cooldown_state(self, trades, now_dt)
        return result

    @wraps(original_roll)
    def roll_session_with_persistence(self, session_date):
        now = datetime.now(UTC)
        previous_session = getattr(self, "_runtime_integrity_session_date", None)

        # First call after startup is initialization, not a new market session.
        # Preserve restored safety state and only prune genuinely expired entries.
        if previous_session is None:
            self._runtime_integrity_session_date = session_date
            self.symbol_cooldowns = {k: v for k, v in self.symbol_cooldowns.items() if v > now}
            self.strategy_cooldowns = {k: v for k, v in self.strategy_cooldowns.items() if v > now}
            active = list(self.symbol_cooldowns.values()) + list(self.strategy_cooldowns.values())
            self.cooldown_until = max(active) if active else None
            if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
                self.daily_halted = False
                self.daily_halt_date = None
            return None

        if session_date <= previous_session:
            return None

        self._runtime_integrity_session_date = session_date
        # Real forward session: reset loss-streak accounting, while preserving any
        # still-live cooldown whose expiry legitimately crosses the boundary.
        self.consecutive_losses = 0
        self.strategy_consecutive_losses.clear()
        self.symbol_cooldowns = {k: v for k, v in self.symbol_cooldowns.items() if v > now}
        self.strategy_cooldowns = {k: v for k, v in self.strategy_cooldowns.items() if v > now}
        active = list(self.symbol_cooldowns.values()) + list(self.strategy_cooldowns.values())
        self.cooldown_until = max(active) if active else None
        if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
            self.daily_halted = False
            self.daily_halt_date = None
        return None

    reconstruct_with_state._runtime_state_installed = True
    RiskManager.reconstruct_cooldowns = reconstruct_with_state
    RiskManager.roll_session = roll_session_with_persistence


def _install_event_calendar_news_bridge() -> None:
    """Let the existing EventCalendar use Alpaca news with its TradingClient."""
    from app.core.calendar import EventCalendar

    if getattr(EventCalendar._fetch_news, "_alpaca_news_bridge_installed", False):
        return
    original_fetch = EventCalendar._fetch_news

    @wraps(original_fetch)
    async def fetch_news_with_bridge(self, symbols, start, end, limit=20):
        client = self._alpaca
        if client is not None and hasattr(client, "get_news"):
            return await original_fetch(self, symbols, start, end, limit)

        api_key = getattr(client, "_api_key", None) if client is not None else None
        api_secret = getattr(client, "_secret_key", None) if client is not None else None
        if not api_key or not api_secret:
            return []

        import requests

        params = {"start": start, "end": end, "limit": limit, "sort": "desc"}
        if symbols:
            params["symbols"] = ",".join(symbols[:10])

        def request_news():
            response = requests.get(
                "https://data.alpaca.markets/v1beta1/news",
                headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
                params=params,
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json().get("news", []) or []

        try:
            return await asyncio.to_thread(request_news)
        except Exception:
            return []

    fetch_news_with_bridge._alpaca_news_bridge_installed = True
    EventCalendar._fetch_news = fetch_news_with_bridge
