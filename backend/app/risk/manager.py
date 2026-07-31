"""Risk manager -- every order passes through here before reaching a broker.

Capital preservation rules, in evaluation order:

1. kill switches: hard halt (max drawdown) and daily halt (max daily loss)
2. loss-streak cooldown
3. session windows: entries blocked near the close; flatten window enforced
4. exposure: max open positions, one position per symbol, per-position cap
5. instrument quality: minimum price and liquidity
6. PDT protection for small live accounts
7. ATR/stop-based position sizing -- risk a fixed % of equity per trade
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from app.config import RiskConfig
from app.core.clock import MarketClock
from app.core.domain import (
    Order,
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
    Signal,
    SignalAction,
    TradeRecord,
)
from app.core.logging import applog

PDT_EQUITY_MIN = 25_000.0
PDT_MAX_DAY_TRADES = 3  # allowed day trades in any rolling 5-business-day window


def is_entry_order(order: Order, positions: list[Position]) -> bool:
    """Determine if a pending order is an entry order or a position-increasing order.

    Differentiates entries from exits, stop-loss orders, take-profit orders,
    and position-reducing orders using the order side and position context.
    """
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
    def __init__(self, config: RiskConfig, clock: MarketClock) -> None:
        self.config = config
        self.clock = clock
        self.hard_halted: bool = False
        self.hard_halt_reason: str = ""
        self.daily_halted: bool = False
        self.daily_halt_date: date | None = None
        self.consecutive_losses: int = 0
        self.cooldown_until: datetime | None = None
        self.strategy_consecutive_losses: dict[str, int] = {}
        self.strategy_cooldowns: dict[str, datetime] = {}
        self.symbol_cooldowns: dict[str, datetime] = {}
        self._pdt_warned = False
        self.core_watchlist: list[str] | None = None

    def get_risk_state(self) -> dict:
        """Return serializable snapshot of safety-critical risk state for atomic persistence."""
        return {
            "consecutive_losses": self.consecutive_losses,
            "strategy_consecutive_losses": dict(self.strategy_consecutive_losses),
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "symbol_cooldowns": {sym: ts.isoformat() for sym, ts in self.symbol_cooldowns.items()},
            "strategy_cooldowns": {strat: ts.isoformat() for strat, ts in self.strategy_cooldowns.items()},
            "hard_halted": self.hard_halted,
            "hard_halt_reason": self.hard_halt_reason,
            "daily_halted": self.daily_halted,
            "daily_halt_date": self.daily_halt_date.isoformat() if self.daily_halt_date else None,
        }

    def restore_risk_state(self, data: dict) -> None:
        """Restore safety-critical risk state from a durable checkpoint."""
        if not isinstance(data, dict):
            return
        self.consecutive_losses = data.get("consecutive_losses", 0)
        self.strategy_consecutive_losses = data.get("strategy_consecutive_losses", {})
        if data.get("cooldown_until"):
            try:
                self.cooldown_until = datetime.fromisoformat(data["cooldown_until"])
            except Exception:
                pass
        if data.get("symbol_cooldowns"):
            for sym, ts_str in data["symbol_cooldowns"].items():
                try:
                    self.symbol_cooldowns[sym] = datetime.fromisoformat(ts_str)
                except Exception:
                    pass
        if data.get("strategy_cooldowns"):
            for strat, ts_str in data["strategy_cooldowns"].items():
                try:
                    self.strategy_cooldowns[strat] = datetime.fromisoformat(ts_str)
                except Exception:
                    pass
        self.hard_halted = bool(data.get("hard_halted", False))
        self.hard_halt_reason = str(data.get("hard_halt_reason") or "")
        self.daily_halted = bool(data.get("daily_halted", False))
        halt_date = data.get("daily_halt_date")
        if halt_date:
            try:
                self.daily_halt_date = date.fromisoformat(halt_date)
            except (TypeError, ValueError):
                self.daily_halt_date = None

    # ---- lifecycle hooks ----

    def on_trade_closed(self, trade: TradeRecord, now: datetime | None = None) -> None:
        strategy = trade.strategy or "manual"
        now_dt = now or datetime.now(UTC)
        if trade.pnl <= 0:
            symbol = trade.symbol.upper()
            self.symbol_cooldowns[symbol] = now_dt + timedelta(minutes=self.config.cooldown_minutes)
            applog.warning(
                "risk",
                f"Loss on {symbol} -- cooling down this ticker for {self.config.cooldown_minutes} minutes"
            )

            current_losses = self.strategy_consecutive_losses.get(strategy, 0) + 1
            self.strategy_consecutive_losses[strategy] = current_losses

            if current_losses >= self.config.consecutive_loss_limit:
                cooldown_time = now_dt + timedelta(minutes=self.config.cooldown_minutes)
                self.strategy_cooldowns[strategy] = cooldown_time
                applog.warning(
                    "risk",
                    f"Strategy '{strategy}' has {current_losses} consecutive losses -- pausing new entries "
                    f"for this strategy for {self.config.cooldown_minutes} minutes",
                )

            self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
            active_cooldowns = [t for t in self.strategy_cooldowns.values() if t > now_dt]
            self.cooldown_until = max(active_cooldowns) if active_cooldowns else None
        else:
            self.strategy_consecutive_losses[strategy] = 0
            self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
            self.strategy_cooldowns = {k: v for k, v in self.strategy_cooldowns.items() if v > now_dt}
            active_cooldowns = list(self.strategy_cooldowns.values())
            self.cooldown_until = max(active_cooldowns) if active_cooldowns else None

    def roll_session(self, session_date) -> None:  # noqa: ANN001
        # A daily halt may clear only on a verified forward session transition.
        # Same-session, stale, backward, or missing halt dates must never fail open.
        if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
            self.daily_halted = False
            self.daily_halt_date = None
            applog.info("risk", "New session -- daily loss halt cleared")
        self.cooldown_until = None
        self.consecutive_losses = 0
        self.strategy_consecutive_losses.clear()
        self.strategy_cooldowns.clear()
        self.symbol_cooldowns.clear()

    def reset_hard_halt(self) -> None:
        self.hard_halted = False
        self.hard_halt_reason = ""
        applog.warning("risk", "Hard halt manually reset -- trading re-enabled")

    # ---- halt checks (engine calls every tick) ----

    def check_halts(self, ctx: AccountContext, now: datetime | None = None) -> str | None:
        """Returns 'daily' or 'hard' when a halt newly trips this tick."""
        if not self.hard_halted and ctx.peak_equity > 0:
            dd_pct = (ctx.peak_equity - ctx.equity) / ctx.peak_equity * 100.0
            if dd_pct >= self.config.max_drawdown_pct:
                self.hard_halted = True
                self.hard_halt_reason = (
                    f"Max drawdown breached: {dd_pct:.1f}% >= {self.config.max_drawdown_pct}%"
                )
                applog.error("risk", f"KILL SWITCH: {self.hard_halt_reason}")
                return "hard"
        if not self.daily_halted and ctx.session_start_equity > 0:
            loss_pct = -ctx.day_pnl / ctx.session_start_equity * 100.0
            if loss_pct >= self.config.max_daily_loss_pct:
                self.daily_halted = True
                self.daily_halt_date = self.clock.session_date(now)
                applog.error(
                    "risk",
                    f"DAILY HALT: day loss {loss_pct:.2f}% >= {self.config.max_daily_loss_pct}% "
                    "-- flattening and stopping for the session",
                )
                return "daily"
        return None

    def in_cooldown(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.cooldown_until is not None and now < self.cooldown_until

    def is_strategy_in_cooldown(self, strategy: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        cooldown_until = self.strategy_cooldowns.get(strategy)
        return cooldown_until is not None and now < cooldown_until

    def should_flatten(self, now: datetime | None = None) -> bool:
        return self.clock.in_flatten_window(self.config.flatten_minutes_before_close, now)

    # ---- entry evaluation ----

    def is_entry_permitted(self, signal: Signal, now: datetime | None = None) -> RiskDecision:
        """Lightweight pre-allocation check for halts, cooldowns, and time windows."""
        cfg = self.config
        symbol = signal.symbol.upper()
        strategy = signal.strategy or "manual"

        if self.hard_halted:
            return RiskDecision(False, f"hard halt active: {self.hard_halt_reason}")
        if self.daily_halted:
            return RiskDecision(False, "daily loss halt active")
        if self.is_strategy_in_cooldown(strategy, now):
            return RiskDecision(False, "loss-streak cooldown active")
        cooldown_until = self.symbol_cooldowns.get(symbol)
        if cooldown_until is not None and (now or datetime.now(UTC)) < cooldown_until:
            return RiskDecision(False, f"ticker-specific cooldown active for {symbol}")
        if not self.clock.in_entry_window(cfg.no_entry_minutes_before_close, now):
            return RiskDecision(False, "outside entry window (market closed or too near close)")
        
        return RiskDecision(True, "permitted")

    def evaluate_entry(
        self,
        signal: Signal,
        ctx: AccountContext,
        bars: pd.DataFrame,
        now: datetime | None = None,
        kelly_override: float | None = None,
    ) -> RiskDecision:
        """Evaluate a proposed entry and return a sizing decision."""
        pre_check = self.is_entry_permitted(signal, now)
        if not pre_check.approved:
            return pre_check
            
        cfg = self.config
        symbol = signal.symbol.upper()
        strategy = signal.strategy or "manual"

        if symbol in [s.upper() for s in ctx.pending_entry_symbols]:
            return RiskDecision(False, f"order already pending for {symbol}")
        if any(p.symbol == symbol for p in ctx.positions):
            return RiskDecision(False, f"position already open in {symbol}")
        if len(ctx.positions) >= cfg.max_open_positions:
            return RiskDecision(False, f"max open positions ({cfg.max_open_positions}) reached")

        from app.portfolio.allocator import _sector
        sector = _sector(symbol)
        sector_count = sum(1 for p in ctx.positions if _sector(p.symbol.upper()) == sector)
        if sector_count >= cfg.max_positions_per_sector:
            return RiskDecision(
                False,
                f"sector limit exceeded: {symbol} is in {sector} which already has {sector_count} open positions (max {cfg.max_positions_per_sector})"
            )

        if bars.empty:
            return RiskDecision(False, "no market data")
        price = signal.price or float(bars["close"].iloc[-1])
        if price < cfg.min_price:
            return RiskDecision(False, f"price {price:.2f} below minimum {cfg.min_price}")
        recent = bars.tail(120)
        avg_dollar_vol = float((recent["close"] * recent["volume"]).mean())
        if pd.isna(avg_dollar_vol) or avg_dollar_vol < cfg.min_avg_dollar_volume:
            return RiskDecision(False, f"illiquid: avg ${avg_dollar_vol:,.0f}/min below minimum")

        if ctx.rolling_day_trades >= PDT_MAX_DAY_TRADES and ctx.equity < PDT_EQUITY_MIN:
            if not ctx.is_paper:
                return RiskDecision(False, "PDT guard: 3 day trades used in rolling window and equity < $25k (live)")
            if not self._pdt_warned:
                self._pdt_warned = True
                applog.warning(
                    "risk",
                    "PDT note: this pace of day trading would exceed the pattern-day-trader "
                    "limit on a live account under $25k",
                )

        stop_loss = signal.stop_loss
        atr = None
        if "atr14" in bars.columns and pd.notna(bars["atr14"].iloc[-1]):
            atr = float(bars["atr14"].iloc[-1])
        if stop_loss is None:
            if atr is None or atr <= 0:
                return RiskDecision(False, "no stop provided and ATR unavailable")
            stop_loss = price - 1.5 * atr if signal.action == SignalAction.ENTER_LONG else price + 1.5 * atr

        if signal.action == SignalAction.ENTER_LONG and stop_loss >= price:
            return RiskDecision(False, f"long stop must be below entry ({stop_loss:.4f} >= {price:.4f})")
        if signal.action == SignalAction.ENTER_SHORT and stop_loss <= price:
            return RiskDecision(False, f"short stop must be above entry ({stop_loss:.4f} <= {price:.4f})")

        stop_distance = abs(price - stop_loss)
        min_distance = max(price * 0.0005, 0.01)
        if stop_distance < min_distance:
            return RiskDecision(False, f"stop too tight ({stop_distance:.4f})")

        risk_mult = getattr(signal, "risk_multiplier", 1.0)
        if kelly_override is not None:
            kelly_cap = getattr(cfg, "kelly_fraction_cap", 0.15)
            effective_allocation = min(kelly_override, kelly_cap) * risk_mult
            target_notional = ctx.equity * effective_allocation
            qty = int(target_notional / price)
            risk_amount = qty * stop_distance
            kelly_used = effective_allocation
        else:
            effective_risk_pct = cfg.risk_per_trade_pct
            explicit_scanner = (getattr(signal, "discovery_source", None) == "scanner")
            legacy_dynamic = (
                not explicit_scanner
                and getattr(signal, "discovery_source", None) is None
                and self.core_watchlist is not None
                and symbol not in self.core_watchlist
            )

            if explicit_scanner:
                scanner_score = getattr(signal, "scanner_score", None)
                scanner_auc = getattr(signal, "scanner_auc", None)
                if scanner_score is not None and scanner_auc is not None:
                    if scanner_score >= 0.85 and scanner_auc >= 0.60:
                        scanner_mult = 1.00
                    elif scanner_score >= 0.70 and scanner_auc >= 0.55:
                        scanner_mult = 0.75
                    else:
                        scanner_mult = 0.50
                else:
                    scanner_mult = 0.50
                effective_risk_pct *= scanner_mult
                applog.info("risk", f"Explicit scanner asset {symbol}: scaling risk by {scanner_mult:.2f} to {effective_risk_pct:.4f}%")
            elif legacy_dynamic:
                effective_risk_pct *= 0.75
                applog.info("risk", f"Legacy dynamic asset {symbol}: scaling risk by 0.75 to {effective_risk_pct:.4f}%")

            effective_risk_pct *= risk_mult
            if risk_mult != 1.0:
                applog.info("risk", f"Dynamic safety risk multiplier applied: scaling risk to {effective_risk_pct}%")

            # Profit Harvesting Sizing (A+ Setups)
            ml_prob = getattr(signal, "ml_probability", None)
            is_harvest_candidate = False
            if ml_prob is not None and ml_prob > 0.85:
                is_harvest_candidate = True
            
            # Check for explosive volume (recent 10 bars vs 120 bars)
            if not is_harvest_candidate and not bars.empty and len(bars) > 60:
                recent_vol = bars["volume"].tail(10).mean()
                avg_vol = bars["volume"].tail(120).mean()
                if avg_vol > 0 and (recent_vol / avg_vol) > 3.0:
                    is_harvest_candidate = True

            if is_harvest_candidate:
                harvest_mult = getattr(cfg, "harvest_risk_multiplier", 2.5)
                effective_risk_pct *= harvest_mult
                applog.info("risk", f"[HARVEST MODE] A+ setup detected for {symbol}. Applying {harvest_mult}x risk multiplier: {effective_risk_pct:.4f}%")

            kelly_used = effective_risk_pct / 100.0
            risk_amount = ctx.equity * kelly_used
            qty = int(risk_amount / stop_distance)

        qty_unconstrained = qty
        max_notional = ctx.equity * cfg.max_position_pct / 100.0
        qty_after_notional = min(qty, int(max_notional / price))
        qty_after_cash = qty_after_notional
        if signal.action == SignalAction.ENTER_LONG:
            qty_after_cash = min(qty_after_notional, int(max(0.0, ctx.cash) / price))

        binding = "none"
        if qty_after_notional < qty_unconstrained:
            binding = f"max_notional_cap ({max_notional:,.0f})"
        if qty_after_cash < qty_after_notional:
            binding = f"cash_cap ({ctx.cash:,.0f})"

        qty = qty_after_cash
        final_notional = qty * price
        applog.info(
            "risk",
            f"[sizing] {symbol} allocator_target={'${:,.0f}'.format(target_notional) if kelly_override is not None else 'n/a (fixed-risk)'} "
            f"unconstrained_qty={qty_unconstrained} "
            f"after_notional_cap={qty_after_notional} "
            f"after_cash_cap={qty_after_cash} "
            f"final_qty={qty} final_notional=${final_notional:,.0f} "
            f"binding_constraint={binding}",
        )
        if qty < 1:
            return RiskDecision(False, "computed size below 1 share (risk budget or cash too small)")

        side = OrderSide.BUY if signal.action == SignalAction.ENTER_LONG else OrderSide.SELL
        take_profit = signal.take_profit
        if take_profit is None:
            take_profit = price + 2.0 * stop_distance if side == OrderSide.BUY else price - 2.0 * stop_distance
        elif side == OrderSide.BUY and take_profit <= price:
            return RiskDecision(False, f"long target must be above entry ({take_profit:.4f} <= {price:.4f})")
        elif side == OrderSide.SELL and take_profit >= price:
            return RiskDecision(False, f"short target must be below entry ({take_profit:.4f} >= {price:.4f})")

        executable_entry = round(float(price), 2)
        executable_stop = round(float(stop_loss), 2)
        executable_target = round(float(take_profit), 2)
        if side == OrderSide.BUY:
            if executable_stop >= executable_entry:
                executable_stop = round(executable_entry - 0.01, 2)
            if executable_target <= executable_entry:
                executable_target = round(executable_entry + 0.01, 2)
        else:
            if executable_stop <= executable_entry:
                executable_stop = round(executable_entry + 0.01, 2)
            if executable_target >= executable_entry:
                executable_target = round(executable_entry - 0.01, 2)
        if executable_stop <= 0 or executable_target <= 0:
            return RiskDecision(False, "broker-valid bracket geometry unavailable at current price")

        order = OrderRequest(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
            stop_loss=executable_stop,
            take_profit=executable_target,
            strategy=signal.strategy,
            reason=signal.reason,
        )
        risk_str = f"${risk_amount:,.0f}" if isinstance(risk_amount, (int, float)) else str(risk_amount)
        return RiskDecision(
            approved=True,
            reason=f"sized {qty} shares, risk {risk_str}",
            order=order,
            kelly_fraction=round(kelly_used, 6) if isinstance(kelly_used, (int, float)) else 0.0,
        )

    def status(self) -> dict:
        return {
            "hard_halted": self.hard_halted,
            "hard_halt_reason": self.hard_halt_reason,
            "daily_halted": self.daily_halted,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "config": self.config.model_dump(),
        }
