"""Production-path integrity guards for dynamic market intelligence."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    _install_event_calendar_news_bridge()
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
        # Champion execution owns the configured ML/Meta thresholds. Contextual
        # expectancy remains available to research paths and is intentionally not
        # allowed to silently rewrite Champion gating policy.
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


def _rebuild_strategy_cooldown_state(risk_manager, trades, now: datetime) -> None:
    """Rebuild active strategy loss streak/cooldown state from durable trade history."""
    cfg = risk_manager.config
    by_strategy: dict[str, list[Any]] = {}
    for trade in trades:
        strategy = getattr(trade, "strategy", None) or "manual"
        if getattr(trade, "exit_at", None) is None:
            continue
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
    future_strategy = [t for t in risk_manager.strategy_cooldowns.values() if t > now]
    future_symbol = [t for t in risk_manager.symbol_cooldowns.values() if t > now]
    active = future_strategy + future_symbol
    risk_manager.cooldown_until = max(active) if active else None


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
        now_dt = now or datetime.now(UTC)
        for trade in trades:
            if getattr(trade, "pnl", 0.0) <= 0:
                _LAST_LOSS_CONTEXT[str(trade.symbol).upper()] = _context_from_trade(trade)
        _rebuild_strategy_cooldown_state(self, trades, now_dt)
        return result

    original_roll = RiskManager.roll_session

    @wraps(original_roll)
    def roll_session_with_persistence(self, session_date):
        """Preserve live cooldowns on restart; only reset streak state on a real session transition."""
        now = datetime.now(UTC)
        previous_session = getattr(self, "_runtime_integrity_session_date", None)
        if previous_session is None:
            self._runtime_integrity_session_date = session_date
            # Startup initialization is not a new session. Preserve restored
            # cooldowns and only prune entries that have genuinely expired.
            self.symbol_cooldowns = {k: v for k, v in self.symbol_cooldowns.items() if v > now}
            self.strategy_cooldowns = {k: v for k, v in self.strategy_cooldowns.items() if v > now}
            if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
                self.daily_halted = False
                self.daily_halt_date = None
            active = list(self.symbol_cooldowns.values()) + list(self.strategy_cooldowns.values())
            self.cooldown_until = max(active) if active else None
            return None

        if session_date <= previous_session:
            return None

        self._runtime_integrity_session_date = session_date
        # Real forward session transition: reset loss streak accounting, but do not
        # destroy a still-live cooldown that legitimately spans the session boundary.
        self.consecutive_losses = 0
        self.strategy_consecutive_losses.clear()
        self.symbol_cooldowns = {k: v for k, v in self.symbol_cooldowns.items() if v > now}
        self.strategy_cooldowns = {k: v for k, v in self.strategy_cooldowns.items() if v > now}
        self.cooldown_until = max(
            list(self.symbol_cooldowns.values()) + list(self.strategy_cooldowns.values()),
            default=None,
        )
        if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
            self.daily_halted = False
            self.daily_halt_date = None
        return None

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
    RiskManager.roll_session = roll_session_with_persistence


def _install_event_calendar_news_bridge() -> None:
    """Make the existing EventCalendar use Alpaca news even when given TradingClient."""
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

        import asyncio
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
            payload = response.json()
            return payload.get("news", []) or []

        try:
            return await asyncio.to_thread(request_news)
        except Exception:
            return []

    fetch_news_with_bridge._alpaca_news_bridge_installed = True
    EventCalendar._fetch_news = fetch_news_with_bridge


def _install_engine_entry_boundary() -> None:
    """Apply durable thesis/news checks to the final candidate boundary."""
    try:
        from app.core.engine_evaluation import TradingEngineEvaluationMixin
    except Exception:
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

            # Structured symbol-specific news is already attached upstream. Refresh
            # it at the final boundary when possible so a breaking catalyst can still
            # affect the decision after the earlier per-tick lookup.
            news_context = None
            try:
                data = getattr(self, "data", None)
                if data is not None and hasattr(data, "get_news_context"):
                    news_context = await data.get_news_context(sig.symbol)
            except Exception:
                news_context = None

            if news_context is not None:
                sig.news_sentiment = float(getattr(news_context, "sentiment", sig.news_sentiment) or 0.0)
                sig.news_direction = str(getattr(news_context, "direction", sig.news_direction) or sig.news_direction)
                sig.news_confidence = float(getattr(news_context, "confidence", sig.news_confidence) or sig.news_confidence)
                sig.news_catalyst = getattr(news_context, "catalyst", sig.news_catalyst)
                sig.news_age_seconds = getattr(news_context, "newest_age_seconds", sig.news_age_seconds)
                sig.news_headline_count = int(getattr(news_context, "headline_count", sig.news_headline_count) or 0)
                sig.news_fresh_headline_count = int(getattr(news_context, "fresh_headline_count", sig.news_fresh_headline_count) or 0)
                sig.news_context_available = sig.news_headline_count > 0

                # Only high-confidence, fresh material catalysts get a hard final veto.
                # Ordinary/stale news remains evidence for the multi-factor context gate.
                age = sig.news_age_seconds
                severe = (
                    sig.news_context_available
                    and sig.news_catalyst in {"financing", "regulatory", "earnings"}
                    and age is not None and age <= 1800
                    and sig.news_confidence >= 0.70
                )
                if severe:
                    if sig.side == "long" and sig.news_direction == "bearish":
                        from app.core.applog import applog
                        applog.info("engine.entry", f"{sig.symbol}: high-confidence fresh adverse catalyst blocks LONG ({sig.news_catalyst})")
                        continue
                    if sig.side == "short" and sig.news_direction == "bullish":
                        from app.core.applog import applog
                        applog.info("engine.entry", f"{sig.symbol}: high-confidence fresh adverse catalyst blocks SHORT ({sig.news_catalyst})")
                        continue
            out.append((sig, df))
        return out

    evaluate_with_entry_intelligence._entry_boundary_installed = True
    TradingEngineEvaluationMixin._evaluate_symbol = evaluate_with_entry_intelligence
