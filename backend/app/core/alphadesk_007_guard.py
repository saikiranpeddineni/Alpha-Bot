"""AlphaDesk 007 production stabilization guardrails.

This module is intentionally centralized so the live engine, backtester, and
paper broker cannot accidentally bypass the final risk invariants introduced
for AlphaDesk 007.

The guard preserves existing interfaces while enforcing:
- one final planned-risk ceiling;
- volatility-aware minimum stop distance;
- lower portfolio heat;
- strategy loss cooldowns;
- daily freeze after a catastrophic realized loss;
- non-zero trade hold telemetry.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from functools import wraps
from typing import Any

_INSTALLED = False

TARGET_RISK_PCT = 0.25
HARD_RISK_CAP_PCT = 0.40
MAX_PORTFOLIO_HEAT_PCT = 2.00
MAX_OPEN_POSITIONS = 5
MAX_DAILY_LOSS_PCT = 1.50
MAX_STRATEGY_CONSECUTIVE_LOSSES = 3
GLOBAL_COOLDOWN_MINUTES = 30
SAME_THESIS_COOLDOWN_MINUTES = 120
MIN_STOP_PCT = 0.10
MIN_STOP_ABS = 0.02
MIN_STOP_ATR = 0.50
CATASTROPHIC_LOSS_R = 1.50


def _set_if_lower(config: Any, name: str, value: float | int) -> None:
    try:
        current = getattr(config, name)
    except AttributeError:
        return
    if isinstance(current, bool):
        return
    try:
        current_f = float(current)
        value_f = float(value)
        if current_f > value_f:
            object.__setattr__(config, name, value)
    except (TypeError, ValueError):
        object.__setattr__(config, name, value)


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
        # Runtime DB settings remain useful, but never above the 007 safety ceiling.
        _set_if_lower(config, "risk_per_trade_pct", TARGET_RISK_PCT)
        _set_if_lower(config, "max_open_positions", MAX_OPEN_POSITIONS)
        _set_if_lower(config, "max_daily_loss_pct", MAX_DAILY_LOSS_PCT)
        _set_if_lower(config, "min_stop_pct", MIN_STOP_PCT)
        _set_if_lower(config, "kelly_fraction_cap", 0.10)
        _set_if_lower(config, "max_positions_per_sector", 2)
        self.config = config
        # These fields already participate in the existing Beast gate.
        object.__setattr__(config, "beast_global_cooldown_minutes", GLOBAL_COOLDOWN_MINUTES)
        object.__setattr__(config, "beast_same_thesis_cooldown_minutes", SAME_THESIS_COOLDOWN_MINUTES)
        object.__setattr__(config, "beast_min_entry_quality", max(float(getattr(config, "beast_min_entry_quality", 82.0)), 82.0))

    @wraps(original_evaluate_entry)
    def guarded_evaluate_entry(self, signal, ctx, bars, now=None, kelly_override=None):
        now = now or datetime.now(UTC)
        try:
            price = float(signal.price or bars["close"].iloc[-1])
            atr = None
            if "atr14" in bars.columns and len(bars) and bars["atr14"].iloc[-1] is not None:
                raw_atr = float(bars["atr14"].iloc[-1])
                atr = raw_atr if math.isfinite(raw_atr) and raw_atr > 0 else None
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
                return RiskDecision(
                    False,
                    f"AlphaDesk 007: stop distance {proposed_distance:.4f} < minimum {minimum_distance:.4f} "
                    f"(price={price:.2f}, atr={atr})",
                )

        decision = original_evaluate_entry(self, signal, ctx, bars, now=now, kelly_override=kelly_override)
        if not decision.approved or decision.order is None:
            return decision

        order = decision.order
        entry_price = float(price)
        stop_price = float(order.stop_loss or proposed_stop or price)
        stop_distance = abs(entry_price - stop_price)
        if stop_distance < minimum_distance:
            return RiskDecision(False, "AlphaDesk 007: executable stop geometry is too tight")

        # Final sizing authority: no strategy score/Kelly multiplier can exceed
        # the 007 planned-risk ceiling.
        max_planned_risk_dollars = ctx.equity * HARD_RISK_CAP_PCT / 100.0
        target_risk_dollars = ctx.equity * TARGET_RISK_PCT / 100.0
        original_risk = abs(float(order.qty)) * stop_distance
        allowed_risk = min(max_planned_risk_dollars, target_risk_dollars)
        max_qty = int(allowed_risk / stop_distance)
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
            return RiskDecision(
                False,
                f"AlphaDesk 007: portfolio heat {new_heat_pct:.2f}% > {MAX_PORTFOLIO_HEAT_PCT:.2f}%",
            )

        effective_fraction = original_risk / max(float(ctx.equity), 1.0)
        decision.kelly_fraction = round(effective_fraction, 6)
        decision.reason = (
            f"AlphaDesk 007 sized {int(order.qty)} shares; planned risk=${original_risk:,.2f} "
            f"({effective_fraction * 100:.3f}% equity); heat={new_heat_pct:.2f}%"
        )
        order.metadata = dict(order.metadata or {})
        order.metadata.update(
            {
                "alphadesk_007_guard": True,
                "planned_risk_pct": round(effective_fraction * 100.0, 4),
                "stop_distance": round(stop_distance, 6),
                "min_stop_distance": round(minimum_distance, 6),
                "portfolio_heat_pct": round(new_heat_pct, 4),
            }
        )
        return decision

    @wraps(original_on_trade_closed)
    def guarded_on_trade_closed(self, trade, now=None):
        result = original_on_trade_closed(self, trade, now=now)
        realized_r = getattr(trade, "r_multiple", None)
        try:
            catastrophic = realized_r is not None and float(realized_r) <= -CATASTROPHIC_LOSS_R
        except (TypeError, ValueError):
            catastrophic = False
        if catastrophic:
            now_dt = now or datetime.now(UTC)
            self.daily_halted = True
            try:
                self.daily_halt_date = self.clock.session_date(now_dt)
            except Exception:
                self.daily_halt_date = now_dt.date()
            self.hard_halt_reason = (
                f"AlphaDesk 007 catastrophic realized loss: {float(realized_r):.2f}R on "
                f"{trade.symbol}; trading frozen for session"
            )
        return result

    @wraps(original_is_strategy_in_cooldown)
    def guarded_is_strategy_in_cooldown(self, strategy, now=None):
        count = int(getattr(self, "strategy_consecutive_losses", {}).get(strategy or "manual", 0))
        return count >= MAX_STRATEGY_CONSECUTIVE_LOSSES

    RiskManager.__init__ = guarded_init
    RiskManager.evaluate_entry = guarded_evaluate_entry
    RiskManager.on_trade_closed = guarded_on_trade_closed
    RiskManager.is_strategy_in_cooldown = guarded_is_strategy_in_cooldown


def _install_trade_telemetry_guard() -> None:
    from app.portfolio.tracker import PortfolioTracker, FillApplicationResult

    original_apply_fill = PortfolioTracker._apply_fill

    @wraps(original_apply_fill)
    def guarded_apply_fill(self, fill, peak_price=None, trough_price=None, initial_stop=None, order=None):
        result = original_apply_fill(
            self,
            fill,
            peak_price=peak_price,
            trough_price=trough_price,
            initial_stop=initial_stop,
            order=order,
        )
        if not isinstance(result, FillApplicationResult) or result.closed_trade is None:
            return result

        trade = result.closed_trade
        updates: dict[str, Any] = {}
        try:
            hold_seconds = max(0.0, (trade.exit_at - trade.entry_at).total_seconds())
            updates["hold_bars"] = max(1, int(math.ceil(hold_seconds / 60.0)))
            metadata = dict(trade.metadata or {})
            metadata.update(
                {
                    "alphadesk_007_guard": True,
                    "hold_seconds": round(hold_seconds, 3),
                    "hold_minutes": round(hold_seconds / 60.0, 3),
                    "economic_trade_key": f"{trade.symbol}:{trade.entry_at.isoformat()}",
                }
            )
            updates["metadata"] = metadata
            if trade.candidate_id is None:
                updates["candidate_id"] = f"{trade.symbol}:{trade.entry_at.isoformat()}"
        except Exception:
            pass

        if updates:
            try:
                enriched = trade.model_copy(update=updates)
                if self.trades and self.trades[-1].id == trade.id:
                    self.trades[-1] = enriched
                result.closed_trade = enriched
            except Exception:
                pass
        return result

    PortfolioTracker._apply_fill = guarded_apply_fill


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_risk_guard()
    _install_trade_telemetry_guard()
    _INSTALLED = True
