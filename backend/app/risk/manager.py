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
from datetime import UTC, datetime, timedelta

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
    # If same side as position, it is an entry/position increase. If opposite, it is an exit.
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
    # Rolling 5-calendar-day day-trade count for PDT guard (does not reset on
    # session roll — supplied by PortfolioTracker.rolling_day_trade_count()).
    rolling_day_trades: int = 0
    is_paper: bool = False
    pending_entry_symbols: list[str] = field(default_factory=list)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    order: OrderRequest | None = None
    kelly_fraction: float | None = None  # fractional Kelly used for sizing (Phase 3)


class RiskManager:
    def __init__(self, config: RiskConfig, clock: MarketClock) -> None:
        self.config = config
        self.clock = clock
        self.hard_halted: bool = False
        self.hard_halt_reason: str = ""
        self.daily_halted: bool = False
        self.daily_halt_date = None
        self.consecutive_losses: int = 0
        self.cooldown_until: datetime | None = None
        self.strategy_consecutive_losses: dict[str, int] = {}
        self.strategy_cooldowns: dict[str, datetime] = {}
        self.symbol_cooldowns: dict[str, datetime] = {}
        self._pdt_warned = False
        self.core_watchlist: list[str] | None = None

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
            
            # Track strategy-specific consecutive losses
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
            
            # Keep legacy fields updated for UI/backward compatibility
            self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
            active_cooldowns = [t for t in self.strategy_cooldowns.values() if t > now_dt]
            self.cooldown_until = max(active_cooldowns) if active_cooldowns else None
        else:
            self.strategy_consecutive_losses[strategy] = 0
            # Keep legacy fields updated
            self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
            # Clean expired cooldowns from dict
            self.strategy_cooldowns = {k: v for k, v in self.strategy_cooldowns.items() if v > now_dt}
            active_cooldowns = list(self.strategy_cooldowns.values())
            self.cooldown_until = max(active_cooldowns) if active_cooldowns else None

    def roll_session(self, session_date) -> None:  # noqa: ANN001
        if self.daily_halted and self.daily_halt_date != session_date:
            self.daily_halted = False
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

    def evaluate_entry(
        self,
        signal: Signal,
        ctx: AccountContext,
        bars: pd.DataFrame,
        now: datetime | None = None,
        kelly_override: float | None = None,
    ) -> RiskDecision:
        """Evaluate a proposed entry and return a sizing decision.

        Parameters
        ----------
        kelly_override:
            When provided by the CapitalAllocationOptimizer, this fractional
            Kelly value (0-1) replaces the fixed ``risk_per_trade_pct`` for
            position sizing. It is capped at ``cfg.kelly_fraction_cap``.
            Pass ``None`` (default) to use the existing fixed-fraction logic.
        """
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
        if symbol in [s.upper() for s in ctx.pending_entry_symbols]:
            return RiskDecision(False, f"order already pending for {symbol}")
        if any(p.symbol == symbol for p in ctx.positions):
            return RiskDecision(False, f"position already open in {symbol}")
        if len(ctx.positions) >= cfg.max_open_positions:
            return RiskDecision(False, f"max open positions ({cfg.max_open_positions}) reached")

        # Sector concentration check
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
        recent = bars.tail(120)  # Smooth out opening volume spikes by checking a 2-hour window instead of 30 mins
        avg_dollar_vol = float((recent["close"] * recent["volume"]).mean())
        if avg_dollar_vol < cfg.min_avg_dollar_volume:
            return RiskDecision(False, f"illiquid: avg ${avg_dollar_vol:,.0f}/min below minimum")

        # PDT protection: block only for small LIVE accounts; warn on paper.
        # Use the rolling 5-calendar-day count so session rollover does not
        # restore consumed PDT capacity.
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

        # ---- stop synthesis & position sizing ----
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

        # Determine effective risk fraction
        risk_mult = getattr(signal, "risk_multiplier", 1.0)
        if kelly_override is not None:
            # Phase 3: fractional Kelly from CapitalAllocationOptimizer
            kelly_cap = getattr(cfg, "kelly_fraction_cap", 0.15)
            effective_allocation = min(kelly_override, kelly_cap) * risk_mult
            target_notional = ctx.equity * effective_allocation
            qty = int(target_notional / price)
            risk_amount = qty * stop_distance
            kelly_used = effective_allocation
        else:
            effective_risk_pct = cfg.risk_per_trade_pct
            if self.core_watchlist is not None and symbol not in self.core_watchlist:
                effective_risk_pct *= 0.5
                applog.info("risk", f"Scanned asset {symbol}: scaling risk down to {effective_risk_pct}% (50% reduction)")
            
            effective_risk_pct *= risk_mult
            if risk_mult != 1.0:
                applog.info("risk", f"Dynamic safety risk multiplier applied: scaling risk to {effective_risk_pct}%")
                
            kelly_used = effective_risk_pct / 100.0
            risk_amount = ctx.equity * kelly_used
            qty = int(risk_amount / stop_distance)

        # --- binding-constraint trace ---
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

        order = OrderRequest(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            strategy=signal.strategy,
            reason=signal.reason,
        )
        return RiskDecision(
            approved=True,
            reason=f"sized {qty} shares, risk ${risk_amount:,.0f}",
            order=order,
            kelly_fraction=round(kelly_used, 6),
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
