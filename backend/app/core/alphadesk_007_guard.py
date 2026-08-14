"""AlphaDesk 007 production guardrails and low-friction entry profile.

The production path keeps only controls that directly protect capital or improve
execution. Research/adaptive gates remain available for analysis but do not get
to silently suppress ordinary production entries.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from functools import wraps
from typing import Any

_INSTALLED = False

# Capital protection stays strict.
TARGET_RISK_PCT = 0.25
HARD_RISK_CAP_PCT = 0.40
MAX_PORTFOLIO_HEAT_PCT = 2.00
MAX_OPEN_POSITIONS = 5
MAX_DAILY_LOSS_PCT = 1.50
MAX_STRATEGY_CONSECUTIVE_LOSSES = 3
GLOBAL_COOLDOWN_MINUTES = 10
SAME_THESIS_COOLDOWN_MINUTES = 60
MIN_STOP_PCT = 0.10
MIN_STOP_ABS = 0.02
MIN_STOP_ATR = 0.50
CATASTROPHIC_LOSS_R = 1.50

# Entry friction is deliberately lower than AlphaDesk 006 because the DB showed
# positive forward opportunity being discarded downstream by stacked gates.
ENTRY_ML_THRESHOLD = 0.50
ENTRY_HARD_ML_CUTOFF = 0.46
ENTRY_SCORE_TREND = 50.0
ENTRY_SCORE_RANGE = 60.0
ENTRY_SCORE_VOLATILE = 55.0
ENTRY_SCORE_EVENT = 65.0
ENTRY_QUALITY_NORMAL = 65.0
ENTRY_QUALITY_EVENT = 70.0
ENTRY_QUALITY_DISCOVERY = 78.0
ENTRY_MAX_VWAP_EXTENSION = 2.25
ENTRY_HARD_VWAP_EXTENSION = 3.50
ENTRY_MAX_ATR_EXTENSION = 1.50
ENTRY_HARD_ATR_EXTENSION = 2.00


def _set_lower(config: Any, name: str, value: float | int) -> None:
    try:
        current = getattr(config, name)
        if isinstance(current, bool):
            return
        if float(current) > float(value):
            object.__setattr__(config, name, value)
    except (AttributeError, TypeError, ValueError):
        try:
            object.__setattr__(config, name, value)
        except Exception:
            pass


def _minimum_stop_distance(price: float, atr: float | None) -> float:
    candidates = [MIN_STOP_ABS, abs(price) * (MIN_STOP_PCT / 100.0)]
    if atr is not None and math.isfinite(atr) and atr > 0:
        candidates.append(abs(atr) * MIN_STOP_ATR)
    return max(candidates)


def _install_risk_guard() -> None:
    from app.risk.manager import RiskDecision, RiskManager

    original_init = RiskManager.__init__
    original_evaluate_entry = RiskManager.evaluate_entry
    original_on_trade_closed = RiskManager.on_trade_closed
    original_is_strategy_in_cooldown = RiskManager.is_strategy_in_cooldown

    @wraps(original_init)
    def guarded_init(self, config, clock):
        original_init(self, config, clock)
        _set_lower(config, "risk_per_trade_pct", TARGET_RISK_PCT)
        _set_lower(config, "max_open_positions", MAX_OPEN_POSITIONS)
        _set_lower(config, "max_daily_loss_pct", MAX_DAILY_LOSS_PCT)
        _set_lower(config, "min_stop_pct", MIN_STOP_PCT)
        _set_lower(config, "kelly_fraction_cap", 0.10)
        _set_lower(config, "max_positions_per_sector", 2)
        _set_lower(config, "ml_confidence_threshold", ENTRY_ML_THRESHOLD)
        _set_lower(config, "ml_hard_reject_threshold", ENTRY_HARD_ML_CUTOFF)
        _set_lower(config, "score_threshold_trend", ENTRY_SCORE_TREND)
        _set_lower(config, "score_threshold_range", ENTRY_SCORE_RANGE)
        _set_lower(config, "score_threshold_volatile", ENTRY_SCORE_VOLATILE)
        _set_lower(config, "score_threshold_event", ENTRY_SCORE_EVENT)
        _set_lower(config, "normal_entry_quality_threshold", ENTRY_QUALITY_NORMAL)
        _set_lower(config, "event_entry_quality_threshold", ENTRY_QUALITY_EVENT)
        _set_lower(config, "price_discovery_entry_quality_threshold", ENTRY_QUALITY_DISCOVERY)
        _set_lower(config, "max_entry_vwap_extension_pct", ENTRY_MAX_VWAP_EXTENSION)
        _set_lower(config, "hard_entry_vwap_extension_pct", ENTRY_HARD_VWAP_EXTENSION)
        _set_lower(config, "max_entry_atr_extension", ENTRY_MAX_ATR_EXTENSION)
        _set_lower(config, "hard_entry_atr_extension", ENTRY_HARD_ATR_EXTENSION)
        object.__setattr__(config, "beast_global_cooldown_minutes", GLOBAL_COOLDOWN_MINUTES)
        object.__setattr__(config, "beast_same_thesis_cooldown_minutes", SAME_THESIS_COOLDOWN_MINUTES)
        object.__setattr__(config, "beast_min_entry_quality", ENTRY_QUALITY_NORMAL)
        self.config = config

    @wraps(original_evaluate_entry)
    def guarded_evaluate_entry(self, signal, ctx, bars, now=None, kelly_override=None):
        now = now or datetime.now(UTC)
        try:
            price = float(signal.price or bars["close"].iloc[-1])
            atr = None
            if "atr14" in bars.columns and len(bars):
                raw = float(bars["atr14"].iloc[-1])
                atr = raw if math.isfinite(raw) and raw > 0 else None
        except Exception:
            return RiskDecision(False, "AlphaDesk 007: invalid entry price/ATR")

        minimum_distance = _minimum_stop_distance(price, atr)
        proposed_stop = getattr(signal, "stop_loss", None)
        if proposed_stop is not None:
            try:
                proposed_distance = abs(price - float(proposed_stop))
            except (TypeError, ValueError):
                proposed_distance = 0.0
            if proposed_distance < minimum_distance:
                return RiskDecision(False, f"AlphaDesk 007: stop distance {proposed_distance:.4f} < minimum {minimum_distance:.4f}")

        decision = original_evaluate_entry(self, signal, ctx, bars, now=now, kelly_override=kelly_override)
        if not decision.approved or decision.order is None:
            return decision

        order = decision.order
        entry_price = price
        stop_price = float(order.stop_loss or proposed_stop or price)
        stop_distance = abs(entry_price - stop_price)
        if stop_distance < minimum_distance:
            return RiskDecision(False, "AlphaDesk 007: executable stop geometry is too tight")

        target_risk = ctx.equity * TARGET_RISK_PCT / 100.0
        original_risk = abs(float(order.qty)) * stop_distance
        max_qty = int(target_risk / stop_distance)
        if max_qty < 1:
            return RiskDecision(False, "AlphaDesk 007: risk budget supports less than one share")
        if float(order.qty) > max_qty:
            order.qty = max_qty
            original_risk = max_qty * stop_distance

        current_heat = 0.0
        for position in ctx.positions:
            if getattr(position, "stop_loss", None) is not None:
                current_heat += abs(float(position.avg_entry_price) - float(position.stop_loss)) * abs(float(position.qty))
            else:
                current_heat += abs(float(position.market_value)) * 0.01
        new_heat_pct = (current_heat + original_risk) / max(float(ctx.equity), 1.0) * 100.0
        if new_heat_pct > MAX_PORTFOLIO_HEAT_PCT:
            return RiskDecision(False, f"AlphaDesk 007: portfolio heat {new_heat_pct:.2f}% > {MAX_PORTFOLIO_HEAT_PCT:.2f}%")

        effective_fraction = original_risk / max(float(ctx.equity), 1.0)
        decision.kelly_fraction = round(effective_fraction, 6)
        decision.reason = f"AlphaDesk 007 sized {int(order.qty)} shares; planned risk=${original_risk:,.2f} ({effective_fraction * 100:.3f}% equity); heat={new_heat_pct:.2f}%"
        order.metadata = dict(order.metadata or {})
        order.metadata.update({
            "alphadesk_007_guard": True,
            "planned_risk_pct": round(effective_fraction * 100.0, 4),
            "stop_distance": round(stop_distance, 6),
            "min_stop_distance": round(minimum_distance, 6),
            "portfolio_heat_pct": round(new_heat_pct, 4),
            "entry_profile": "LOW_FRICTION_PRODUCTION",
        })
        return decision

    @wraps(original_on_trade_closed)
    def guarded_on_trade_closed(self, trade, now=None):
        result = original_on_trade_closed(self, trade, now=now)
        try:
            catastrophic = getattr(trade, "r_multiple", None) is not None and float(trade.r_multiple) <= -CATASTROPHIC_LOSS_R
        except (TypeError, ValueError):
            catastrophic = False
        if catastrophic:
            now_dt = now or datetime.now(UTC)
            self.daily_halted = True
            try:
                self.daily_halt_date = self.clock.session_date(now_dt)
            except Exception:
                self.daily_halt_date = now_dt.date()
            self.hard_halt_reason = f"AlphaDesk 007 catastrophic loss: {float(trade.r_multiple):.2f}R on {trade.symbol}; session frozen"
        return result

    @wraps(original_is_strategy_in_cooldown)
    def guarded_is_strategy_in_cooldown(self, strategy, now=None):
        count = int(getattr(self, "strategy_consecutive_losses", {}).get(strategy or "manual", 0))
        return count >= MAX_STRATEGY_CONSECUTIVE_LOSSES

    RiskManager.__init__ = guarded_init
    RiskManager.evaluate_entry = guarded_evaluate_entry
    RiskManager.on_trade_closed = guarded_on_trade_closed
    RiskManager.is_strategy_in_cooldown = guarded_is_strategy_in_cooldown


def _install_pipeline_simplification() -> None:
    from app.strategies.pipeline import SignalPipeline

    if getattr(SignalPipeline, "_alphadesk_007_simplified", False):
        return
    original_init = SignalPipeline.__init__
    original_process = SignalPipeline.process

    @wraps(original_init)
    def guarded_pipeline_init(self, *args, **kwargs):
        kwargs["soft_composite_gating"] = True
        original_init(self, *args, **kwargs)

    @wraps(original_process)
    def guarded_pipeline_process(self, symbol, df, signals, regime, features=None, sector_strength=None, adaptive_ml_thresholds=None):
        return original_process(self, symbol, df, signals, regime, features=features, sector_strength=sector_strength, adaptive_ml_thresholds=None)

    SignalPipeline.__init__ = guarded_pipeline_init
    SignalPipeline.process = guarded_pipeline_process
    SignalPipeline._alphadesk_007_simplified = True


def _install_trade_telemetry_guard() -> None:
    from app.portfolio.tracker import PortfolioTracker, FillApplicationResult
    original_apply_fill = PortfolioTracker._apply_fill
    if getattr(PortfolioTracker, "_alphadesk_007_telemetry", False):
        return

    @wraps(original_apply_fill)
    def guarded_apply_fill(self, fill, peak_price=None, trough_price=None, initial_stop=None, order=None):
        result = original_apply_fill(self, fill, peak_price=peak_price, trough_price=trough_price, initial_stop=initial_stop, order=order)
        if not isinstance(result, FillApplicationResult) or result.closed_trade is None:
            return result
        trade = result.closed_trade
        try:
            hold_seconds = max(0.0, (trade.exit_at - trade.entry_at).total_seconds())
            enriched = trade.model_copy(update={
                "hold_bars": max(1, int(math.ceil(hold_seconds / 60.0))),
                "metadata": {**dict(trade.metadata or {}), "alphadesk_007_guard": True, "hold_seconds": round(hold_seconds, 3), "hold_minutes": round(hold_seconds / 60.0, 3), "economic_trade_key": f"{trade.symbol}:{trade.entry_at.isoformat()}"},
            })
            if self.trades and self.trades[-1].id == trade.id:
                self.trades[-1] = enriched
            result.closed_trade = enriched
        except Exception:
            pass
        return result

    PortfolioTracker._apply_fill = guarded_apply_fill
    PortfolioTracker._alphadesk_007_telemetry = True


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_risk_guard()
    _install_pipeline_simplification()
    _install_trade_telemetry_guard()
    _INSTALLED = True
