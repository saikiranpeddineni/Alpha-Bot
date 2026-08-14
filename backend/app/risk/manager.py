"""Compact AlphaDesk 007 risk authority.

Risk has only two jobs in production:
1. decide whether a trade is executable;
2. calculate quantity from account risk and the structural stop.

Strategy quality, ML, thesis memory, Kelly, behavioral scoring and historical
losses do not belong here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import hashlib
import json
import math

import pandas as pd

from app.config import RiskConfig
from app.core.clock import MarketClock
from app.core.domain import Order, OrderRequest, OrderRole, OrderSide, OrderType, Position, Signal, SignalAction, TradeRecord
from app.core.applog import applog

PDT_EQUITY_MIN = 25_000.0
PDT_MAX_DAY_TRADES = 3


def ensure_thesis_fingerprint(signal: Signal) -> str:
    existing = getattr(signal, "thesis_fingerprint", None)
    if existing:
        return existing
    payload = {
        "symbol": signal.symbol.upper(),
        "action": getattr(signal.action, "value", str(signal.action)),
        "strategy": signal.strategy or "",
        "regime": getattr(getattr(signal, "regime", None), "value", str(getattr(signal, "regime", ""))),
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    signal.thesis_fingerprint = fingerprint
    return fingerprint


def is_entry_order(order: Order, positions: list[Position]) -> bool:
    if getattr(order, "leg", None) in ("stop", "target", "exit"):
        return False
    pos = next((p for p in positions if p.symbol.upper() == order.symbol.upper()), None)
    if pos is None:
        return True
    if pos.qty > 0 and order.side == OrderSide.SELL:
        return False
    if pos.qty < 0 and order.side == OrderSide.BUY:
        return False
    return True


@dataclass
class AccountContext:
    equity: float
    cash: float
    positions: list[Position]
    day_pnl: float
    session_start_equity: float
    peak_equity: float
    day_trades_today: int
    rolling_day_trades: int = 0
    is_paper: bool = False
    pending_entry_symbols: list[str] = field(default_factory=list)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    order: OrderRequest | None = None
    kelly_fraction: float | None = None


class RiskManager:
    """Single production risk/sizing authority."""

    def __init__(self, config: RiskConfig, clock: MarketClock) -> None:
        self.config = config
        self.clock = clock
        self.hard_halted = False
        self.hard_halt_reason = ""
        self.daily_halted = False
        self.daily_halt_date: date | None = None
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None
        self.global_cooldown_until: datetime | None = None
        self.strategy_consecutive_losses: dict[str, int] = {}
        self.strategy_cooldowns: dict[str, datetime] = {}
        self.symbol_cooldowns: dict[str, datetime] = {}
        self.behavioral_loop_cooldowns: dict[str, datetime] = {}
        self.historical_loss_context: dict[str, dict] = {}
        self.closed_theses: dict[str, datetime] = {}
        self.core_watchlist: list[str] | None = None
        self._pdt_warned = False

    def get_risk_state(self) -> dict:
        return {
            "consecutive_losses": self.consecutive_losses,
            "strategy_consecutive_losses": dict(self.strategy_consecutive_losses),
            "cooldown_until": None,
            "global_cooldown_until": None,
            "symbol_cooldowns": {},
            "strategy_cooldowns": {},
            "behavioral_loop_cooldowns": {},
            "historical_loss_context": dict(self.historical_loss_context),
            "hard_halted": self.hard_halted,
            "hard_halt_reason": self.hard_halt_reason,
            "daily_halted": self.daily_halted,
            "daily_halt_date": self.daily_halt_date.isoformat() if self.daily_halt_date else None,
            "closed_theses": {},
        }

    def restore_risk_state(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        self.consecutive_losses = int(data.get("consecutive_losses", 0) or 0)
        self.strategy_consecutive_losses = dict(data.get("strategy_consecutive_losses", {}) or {})
        self.historical_loss_context = dict(data.get("historical_loss_context", {}) or {})
        self.hard_halted = bool(data.get("hard_halted", False))
        self.hard_halt_reason = str(data.get("hard_halt_reason", ""))
        self.daily_halted = bool(data.get("daily_halted", False))
        raw_date = data.get("daily_halt_date")
        if raw_date:
            try:
                self.daily_halt_date = date.fromisoformat(raw_date)
            except Exception:
                self.daily_halt_date = None
        self.cooldown_until = None
        self.global_cooldown_until = None
        self.symbol_cooldowns.clear()
        self.strategy_cooldowns.clear()
        self.behavioral_loop_cooldowns.clear()
        self.closed_theses.clear()

    def reconstruct_cooldowns(self, trades: list[TradeRecord], now: datetime | None = None) -> None:
        self.historical_loss_context = {}
        for trade in trades:
            if getattr(trade, "pnl", 0.0) < 0:
                self.historical_loss_context.setdefault(trade.symbol.upper(), {"losses": 0})["losses"] += 1

    def on_trade_closed(self, trade: TradeRecord, now: datetime | None = None) -> None:
        strategy = trade.strategy or "manual"
        if trade.pnl < 0:
            self.strategy_consecutive_losses[strategy] = self.strategy_consecutive_losses.get(strategy, 0) + 1
            self.historical_loss_context.setdefault(trade.symbol.upper(), {"losses": 0})["losses"] += 1
        else:
            self.strategy_consecutive_losses[strategy] = 0
        self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)

    def roll_session(self, session_date) -> None:  # noqa: ANN001
        if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
            self.daily_halted = False
            self.daily_halt_date = None
        self.consecutive_losses = 0
        self.strategy_consecutive_losses.clear()

    def reset_hard_halt(self) -> None:
        self.hard_halted = False
        self.hard_halt_reason = ""

    def check_halts(self, ctx: AccountContext, now: datetime | None = None) -> str | None:
        now = now or datetime.now(UTC)
        if not self.hard_halted and ctx.peak_equity > 0:
            dd_pct = (ctx.peak_equity - ctx.equity) / ctx.peak_equity * 100.0
            if dd_pct >= self.config.max_drawdown_pct:
                self.hard_halted = True
                self.hard_halt_reason = f"Max drawdown breached: {dd_pct:.2f}% >= {self.config.max_drawdown_pct:.2f}%"
                applog.error("risk", self.hard_halt_reason)
                return "hard"
        if not self.daily_halted and ctx.session_start_equity > 0:
            loss_pct = -ctx.day_pnl / ctx.session_start_equity * 100.0
            if loss_pct >= self.config.max_daily_loss_pct:
                self.daily_halted = True
                self.daily_halt_date = self.clock.session_date(now)
                applog.error("risk", f"Daily loss halt: {loss_pct:.2f}% >= {self.config.max_daily_loss_pct:.2f}%")
                return "daily"
        return None

    def in_cooldown(self, now: datetime | None = None) -> bool:
        return False

    def is_strategy_in_cooldown(self, strategy: str, now: datetime | None = None) -> bool:
        return False

    def should_flatten(self, now: datetime | None = None) -> bool:
        return self.clock.in_flatten_window(self.config.flatten_minutes_before_close, now)

    def is_entry_permitted(self, signal: Signal, now: datetime | None = None) -> RiskDecision:
        now = now or datetime.now(UTC)
        if self.hard_halted:
            return RiskDecision(False, f"hard halt active: {self.hard_halt_reason}")
        if self.daily_halted:
            return RiskDecision(False, "daily loss halt active")
        if not self.clock.in_entry_window(self.config.no_entry_minutes_before_close, now):
            return RiskDecision(False, "outside entry window")
        return RiskDecision(True, "permitted")

    def evaluate_entry(self, signal: Signal, ctx: AccountContext, bars: pd.DataFrame, now: datetime | None = None, kelly_override: float | None = None) -> RiskDecision:
        pre = self.is_entry_permitted(signal, now)
        if not pre.approved:
            return pre
        cfg = self.config
        symbol = signal.symbol.upper()
        if symbol in {s.upper() for s in ctx.pending_entry_symbols}:
            return RiskDecision(False, f"order already pending for {symbol}")
        if any(p.symbol.upper() == symbol for p in ctx.positions):
            return RiskDecision(False, f"position already open in {symbol}")
        if len(ctx.positions) >= cfg.max_open_positions:
            return RiskDecision(False, f"max open positions ({cfg.max_open_positions}) reached")
        if bars.empty:
            return RiskDecision(False, "no market data")

        price = float(signal.price or bars["close"].iloc[-1])
        if price < cfg.min_price:
            return RiskDecision(False, f"price {price:.2f} below minimum {cfg.min_price:.2f}")

        recent = bars.tail(120)
        if "volume" in recent.columns:
            dollar_volume = float((recent["close"] * recent["volume"]).mean())
            if not math.isfinite(dollar_volume) or dollar_volume < cfg.min_avg_dollar_volume:
                return RiskDecision(False, f"illiquid: ${dollar_volume:,.0f}/min")

        if ctx.rolling_day_trades >= PDT_MAX_DAY_TRADES and ctx.equity < PDT_EQUITY_MIN and not ctx.is_paper:
            return RiskDecision(False, "PDT guard: day-trade limit reached")

        atr = float(bars["atr14"].iloc[-1]) if "atr14" in bars.columns and pd.notna(bars["atr14"].iloc[-1]) else None
        stop = float(signal.stop_loss) if signal.stop_loss is not None else 0.0
        if stop <= 0:
            if atr is None or atr <= 0:
                return RiskDecision(False, "no valid stop and ATR unavailable")
            stop = price - 1.5 * atr if signal.action == SignalAction.ENTER_LONG else price + 1.5 * atr

        if signal.action == SignalAction.ENTER_LONG and stop >= price:
            return RiskDecision(False, "long stop must be below entry")
        if signal.action == SignalAction.ENTER_SHORT and stop <= price:
            return RiskDecision(False, "short stop must be above entry")

        stop_distance = abs(price - stop)
        minimum_stop = max(price * (float(cfg.min_stop_pct) / 100.0), 0.01)
        if stop_distance < minimum_stop:
            return RiskDecision(False, f"stop too tight: {stop_distance:.4f} < {minimum_stop:.4f}")

        risk_pct = min(float(cfg.risk_per_trade_pct), 0.25)
        risk_pct *= max(0.1, min(float(getattr(signal, "risk_multiplier", 1.0)), 1.0))
        risk_amount = max(0.0, ctx.equity * risk_pct / 100.0)
        risk_qty = risk_amount / stop_distance
        notional_cap = ctx.equity * float(cfg.max_position_pct) / 100.0
        notional_qty = notional_cap / price if price > 0 else 0.0
        cash_qty = max(0.0, ctx.cash * 0.95 / price) if price > 0 else 0.0
        qty = math.floor(max(0.0, min(risk_qty, notional_qty, cash_qty)))
        if qty < 1:
            return RiskDecision(False, "risk budget produces less than one share")

        target = signal.take_profit
        if target is None and atr is not None and atr > 0:
            target = price + 2.0 * atr if signal.action == SignalAction.ENTER_LONG else price - 2.0 * atr

        side = OrderSide.BUY if signal.action == SignalAction.ENTER_LONG else OrderSide.SELL
        order_type = OrderType.LIMIT if cfg.entry_order_type == "limit" else OrderType.MARKET
        request = OrderRequest(
            symbol=symbol,
            side=side,
            qty=float(qty),
            order_type=order_type,
            limit_price=price if order_type == OrderType.LIMIT else None,
            stop_loss=stop,
            take_profit=target,
            strategy=signal.strategy or "manual",
            reason=signal.reason or "production entry",
            role=OrderRole.ENTRY,
            signal_timestamp_utc=getattr(signal, "ts", None),
            decision_bar_timestamp_utc=getattr(signal, "decision_bar_timestamp_utc", None),
            thesis_fingerprint=getattr(signal, "thesis_fingerprint", None),
            signal_generated_timestamp_utc=getattr(signal, "signal_generated_timestamp_utc", None),
            decision_completed_timestamp_utc=datetime.now(UTC),
            entry_quality_score=getattr(signal, "entry_quality_score", None) or (signal.strategy_params or {}).get("entry_quality_score"),
            entry_quality=getattr(signal, "entry_quality", None) or (signal.strategy_params or {}).get("entry_quality"),
            opportunity_state=getattr(signal, "opportunity_state", None) or (signal.strategy_params or {}).get("opportunity_state"),
            opportunity_score=getattr(signal, "opportunity_score", None) or (signal.strategy_params or {}).get("opportunity_score"),
            vwap_distance_pct=getattr(signal, "vwap_distance_pct", None) or (signal.strategy_params or {}).get("vwap_distance_pct"),
            atr_extension=getattr(signal, "atr_extension", None) or (signal.strategy_params or {}).get("atr_extension"),
            pre_entry_impulse_pct=getattr(signal, "pre_entry_impulse_pct", None),
            event_risk=getattr(signal, "event_risk", "normal"),
            metadata={"candidate_id": getattr(signal, "candidate_id", None), "risk_pct": risk_pct, "risk_amount": risk_amount},
        )
        return RiskDecision(True, f"sized {qty} shares; risk ${risk_amount:,.2f}", order=request, kelly_fraction=0.0)

    def status(self) -> dict:
        return {
            "hard_halted": self.hard_halted,
            "hard_halt_reason": self.hard_halt_reason,
            "daily_halted": self.daily_halted,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": None,
            "global_cooldown_until": None,
            "behavioral_loop_cooldowns": {},
            "historical_loss_context": dict(self.historical_loss_context),
            "config": self.config.model_dump(),
        }
