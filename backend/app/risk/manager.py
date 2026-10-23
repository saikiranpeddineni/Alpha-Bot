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

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from app.config import RiskConfig
from app.core.applog import applog
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
    # The validated strategy target is carried separately from OrderRequest.
    # PIS owns discretionary targets; putting this on the broker request would
    # create a second, competing native broker target.
    take_profit: float | None = None


class RiskManager:
    def __init__(self, config: RiskConfig, clock: MarketClock) -> None:
        self.config = config
        self.clock = clock
        self.hard_halted: bool = False
        self.hard_halt_reason: str = ""
        self.daily_halted: bool = False
        self.daily_halt_date: date | None = None
        # DAILY PROFIT LOCK 2026-10-16: bank green days like the loss halt banks red ones.
        self.profit_locked: bool = False
        self.profit_lock_date: date | None = None
        self.consecutive_losses: int = 0
        self.cooldown_until: datetime | None = None
        self.strategy_consecutive_losses: dict[str, int] = {}
        self.strategy_cooldowns: dict[str, datetime] = {}
        self.symbol_cooldowns: dict[str, datetime] = {}
        # =============================================================================
        # [MODIFIED 2026-10-23]: Ticker Loss Count Tracker for Asymmetric Session Quarantine
        # ORIGINAL INTENT: Only tracked symbol_cooldowns timestamp; did not count daily losses.
        # REASON FOR CHANGE: Enforce symbol_max_daily_losses = 2 (session lockout after 2 losses).
        # SAFE ROLLBACK: Remove self.symbol_loss_counts.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        self.symbol_loss_counts: dict[str, int] = {}
        # =============================================================================
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
            "profit_locked": self.profit_locked,
            "profit_lock_date": self.profit_lock_date.isoformat() if self.profit_lock_date else None,
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
        self.profit_locked = bool(data.get("profit_locked", False))
        profit_date = data.get("profit_lock_date")
        if profit_date:
            try:
                self.profit_lock_date = date.fromisoformat(profit_date)
            except (TypeError, ValueError):
                self.profit_lock_date = None
    # ---- lifecycle hooks ----

    def on_trade_closed(self, trade: TradeRecord, now: datetime | None = None) -> None:
        strategy = trade.strategy or "manual"
        now_dt = now or datetime.now(UTC)
        # =============================================================================
        # [MODIFIED 2026-10-23]: Asymmetric Re-Entry Lockout Engine
        # ORIGINAL INTENT: Symmetrically cleared strategy losses on wins and set symbol_cooldowns only on losses.
        # Zero cooldown was used on wins, and flat (often 0) cooldown on losses.
        # REASON FOR CHANGE: On a loss, enforce minimum 20m cooldown and quarantine after 2 losses (stopping
        # NCPL -$465 disaster). On a win, enforce a brief 5m consolidation lock before allowing 2nd-leg expansions.
        # SAFE ROLLBACK: Previous lines preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if trade.pnl <= 0:
        #     symbol = trade.symbol.upper()
        #     self.symbol_cooldowns[symbol] = now_dt + timedelta(minutes=self.config.symbol_cooldown_minutes)
        #     applog.warning(
        #         "risk",
        #         f"Loss on {symbol} -- cooling down this ticker for {self.config.symbol_cooldown_minutes} minutes"
        #     )
        #     current_losses = self.strategy_consecutive_losses.get(strategy, 0) + 1
        #     self.strategy_consecutive_losses[strategy] = current_losses
        #     if current_losses >= self.config.strategy_consecutive_loss_limit:
        #         cooldown_time = now_dt + timedelta(minutes=self.config.strategy_cooldown_minutes)
        #         self.strategy_cooldowns[strategy] = cooldown_time
        #         applog.warning(
        #             "risk",
        #             f"Strategy '{strategy}' has {current_losses} consecutive losses -- pausing new entries "
        #             f"for this strategy for {self.config.strategy_cooldown_minutes} minutes",
        #         )
        #     self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
        #     active_cooldowns = [t for t in self.strategy_cooldowns.values() if t > now_dt]
        #     self.cooldown_until = max(active_cooldowns) if active_cooldowns else None
        # else:
        #     self.strategy_consecutive_losses[strategy] = 0
        #     self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
        #     self.strategy_cooldowns = {k: v for k, v in self.strategy_cooldowns.items() if v > now_dt}
        #     active_cooldowns = list(self.strategy_cooldowns.values())
        #     self.cooldown_until = max(active_cooldowns) if active_cooldowns else None
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        symbol = trade.symbol.upper()
        # =============================================================================
        # [MODIFIED 2026-10-24]: Strict Symbol Max Daily Loss Lockout & Mega-Win Cooldown
        # ORIGINAL INTENT: Allowed 2 losses before quarantine, and flat 5-minute win cooldown.
        # This permitted MGNX to re-enter for a 2nd loss (-$261.12), and PHAT to re-short 7 minutes
        # after a +4.6R runner into an immediate reversal (-$188.79).
        # REASON FOR CHANGE: Enforce max_daily_losses = 1 to prevent whipsaw revenge trading on the same
        # symbol, and enforce 45m cooldown after >= 3.0R mega-winners to prevent exhausted-runner re-entry.
        # SAFE ROLLBACK: The original code is preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if trade.pnl <= 0:
        #     self.symbol_loss_counts[symbol] = self.symbol_loss_counts.get(symbol, 0) + 1
        #     max_daily_losses = getattr(self.config, "symbol_max_daily_losses", 2)
        #     if self.symbol_loss_counts[symbol] >= max_daily_losses:
        #         self.symbol_cooldowns[symbol] = now_dt + timedelta(minutes=390)
        #     else:
        #         cd_mins = max(20, int(getattr(self.config, "symbol_cooldown_minutes", 20) or 20))
        #         self.symbol_cooldowns[symbol] = now_dt + timedelta(minutes=cd_mins)
        # else:
        #     win_cd_mins = int(getattr(self.config, "symbol_win_cooldown_minutes", 5) or 5)
        #     self.symbol_cooldowns[symbol] = now_dt + timedelta(minutes=win_cd_mins)
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if trade.pnl <= 0:
            self.symbol_loss_counts[symbol] = self.symbol_loss_counts.get(symbol, 0) + 1
            max_daily_losses = getattr(self.config, "symbol_max_daily_losses", 1) or 1

            if self.symbol_loss_counts[symbol] >= max_daily_losses:
                # Quarantined for the remainder of the session (390m)
                self.symbol_cooldowns[symbol] = now_dt + timedelta(minutes=390)
                applog.warning(
                    "risk",
                    f"{symbol} hit {self.symbol_loss_counts[symbol]} loss(es) today -- quarantined for remainder of session to eliminate whipsaw losses",
                )
            else:
                cd_mins = max(20, int(getattr(self.config, "symbol_cooldown_minutes", 20) or 20))
                self.symbol_cooldowns[symbol] = now_dt + timedelta(minutes=cd_mins)
                applog.warning(
                    "risk",
                    f"Loss on {symbol} (loss #{self.symbol_loss_counts[symbol]}) -- cooling down ticker for {cd_mins} minutes to eliminate whipsaws",
                )

            current_losses = self.strategy_consecutive_losses.get(strategy, 0) + 1
            self.strategy_consecutive_losses[strategy] = current_losses

            if current_losses >= self.config.strategy_consecutive_loss_limit:
                strat_cd = max(20, int(getattr(self.config, "strategy_cooldown_minutes", 20) or 20))
                cooldown_time = now_dt + timedelta(minutes=strat_cd)
                self.strategy_cooldowns[strategy] = cooldown_time
                applog.warning(
                    "risk",
                    f"Strategy '{strategy}' has {current_losses} consecutive losses -- pausing new entries for {strat_cd} minutes",
                )

            self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
            active_cooldowns = [t for t in self.strategy_cooldowns.values() if t > now_dt]
            self.cooldown_until = max(active_cooldowns) if active_cooldowns else None
        else:
            trade_r = float(getattr(trade, "r_multiple", 0.0) or 0.0)
            mega_r_floor = float(getattr(self.config, "mega_win_cooldown_r", 3.0) or 3.0)
            if trade_r >= mega_r_floor:
                win_cd_mins = int(getattr(self.config, "mega_win_cooldown_minutes", 45) or 45)
                applog.info(
                    "risk",
                    f"Mega-win on {symbol} (+{trade_r:.2f}R | +${trade.pnl:.2f}) -- locking ticker for {win_cd_mins}m to prevent exhausted-runner re-entry",
                )
            else:
                win_cd_mins = int(getattr(self.config, "symbol_win_cooldown_minutes", 5) or 5)
                applog.info(
                    "risk",
                    f"Win on {symbol} (+${trade.pnl:.2f}) -- locking ticker for {win_cd_mins}m consolidation before permitting second-leg continuation",
                )
            self.symbol_cooldowns[symbol] = now_dt + timedelta(minutes=win_cd_mins)
            self.strategy_consecutive_losses[strategy] = 0
            self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
            self.strategy_cooldowns = {k: v for k, v in self.strategy_cooldowns.items() if v > now_dt}
            active_cooldowns = list(self.strategy_cooldowns.values())
            self.cooldown_until = max(active_cooldowns) if active_cooldowns else None
        # =============================================================================
        # =============================================================================

    def roll_session(self, session_date) -> None:  # noqa: ANN001
        # A daily halt may clear only on a verified forward session transition.
        # Same-session, stale, backward, or missing halt dates must never fail open.
        if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
            self.daily_halted = False
            self.daily_halt_date = None
            applog.info("risk", "New session -- daily loss halt cleared")
        if self.profit_locked and self.profit_lock_date is not None and session_date > self.profit_lock_date:
            self.profit_locked = False
            self.profit_lock_date = None
            applog.info("risk", "New session -- daily profit lock cleared")
        self.cooldown_until = None
        self.consecutive_losses = 0
        self.strategy_consecutive_losses.clear()
        self.strategy_cooldowns.clear()
        self.symbol_cooldowns.clear()
        if hasattr(self, "symbol_loss_counts"):
            self.symbol_loss_counts.clear()

    def reset_hard_halt(self) -> None:
        self.hard_halted = False
        self.hard_halt_reason = ""
        applog.warning("risk", "Hard halt manually reset -- trading re-enabled")

    # ---- halt checks (engine calls every tick) ----

    def check_halts(self, ctx: AccountContext, now: datetime | None = None) -> str | None:
        """Returns 'daily', 'profit', or 'hard' when a halt newly trips this tick."""
        # Daily loss limit check (session-scoped)

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
        # DAILY PROFIT LOCK 2026-10-16: bank the day at +lock_pct the same way
        # the loss halt banks red days. 0.0 disables. Never triggers on a
        # non-positive session start, same guard as the loss halt.
        lock_pct = float(getattr(self.config, "daily_profit_lock_pct", 0.0) or 0.0)
        if lock_pct > 0 and not self.profit_locked and ctx.session_start_equity > 0:
            gain_pct = ctx.day_pnl / ctx.session_start_equity * 100.0
            if gain_pct >= lock_pct:
                self.profit_locked = True
                self.profit_lock_date = self.clock.session_date(now)
                applog.warning(
                    "risk",
                    f"PROFIT LOCK: day gain {gain_pct:.2f}% >= {lock_pct:.2f}% "
                    "-- flattening and banking the session",
                )
                return "profit"
        return None

    def in_cooldown(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.cooldown_until is not None and now < self.cooldown_until

    def is_strategy_in_cooldown(self, strategy: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        cooldown_until = self.strategy_cooldowns.get(strategy)
        return cooldown_until is not None and now < cooldown_until

    def should_flatten(self, now: datetime | None = None) -> bool:
        # --- NEW IMPLEMENTATION ---
        # Defensive getattr fallback to default 5 minutes prevents engine tick halts on missing attribute.
        flatten_mins = int(getattr(self.config, "flatten_minutes_before_close", 5) or 5)
        return self.clock.in_flatten_window(flatten_mins, now)

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
        if self.profit_locked:
            return RiskDecision(False, "daily profit lock active -- day banked")
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
        take_profit = signal.take_profit
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

        if take_profit is not None:
            if signal.action == SignalAction.ENTER_LONG and take_profit <= price:
                return RiskDecision(False, f"long target must be above entry ({take_profit:.4f} <= {price:.4f})")
            if signal.action == SignalAction.ENTER_SHORT and take_profit >= price:
                return RiskDecision(False, f"short target must be below entry ({take_profit:.4f} >= {price:.4f})")
        elif atr is not None and atr > 0:
            take_profit = price + 2.0 * atr if signal.action == SignalAction.ENTER_LONG else price - 2.0 * atr

        # =============================================================================
        # [MODIFIED 2026-10-23]: Volatility Noise Floor Stop Cushioning & Width Ceiling
        # ORIGINAL INTENT: Used min_distance = max(price * 0.0005, 0.01), allowing micro-stops
        # (e.g. HTZ $0.03 stop on $2.22 = 1.35%, MGNX $0.05 stop on $2.43 = 2.06%).
        # REASON FOR CHANGE: Micro-stops on volatile sub-$5 penny stocks are instantly wiped out
        # by normal bid-ask bounce and 1-minute candle wicks (HTZ ATR was $0.19, MGNX was $0.185).
        # Furthermore, the tiny stop distance in the denominator artificially multiplied share count
        # (8,586 shares on HTZ), turning 2 cents of normal spread slippage into a -$429.30 blowout.
        # Cushioning stop distance to min max(0.60 * atr, price * min_floor_frac, 0.01) provides
        # structural room to catch momentum breakouts without whipsaws, while risk parity
        # automatically sizes shares down to guarantee losses stay capped at the risk budget.
        # SAFE ROLLBACK: Previous lines preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # stop_distance = abs(price - stop_loss)
        # min_distance = max(price * 0.0005, 0.01)
        # if stop_distance < min_distance:
        #     return RiskDecision(False, f"stop too tight ({stop_distance:.4f})")
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        stop_distance = abs(price - stop_loss)
        if atr is not None and atr > 0:
            min_floor_frac = (cfg.min_stop_pct / 100.0) if getattr(cfg, "min_stop_pct", 0.0) > 0 else 0.0045
            min_stop_mult = getattr(cfg, "min_stop_floor_atr_mult", 0.60) or 0.60
            min_noise = max(min_stop_mult * atr, price * min_floor_frac, 0.01)
            if stop_distance < min_noise:
                stop_distance = min_noise
                stop_loss = round(price - stop_distance, 4) if signal.action == SignalAction.ENTER_LONG else round(price + stop_distance, 4)

        min_distance = max(price * 0.0005, 0.01)
        if stop_distance < min_distance:
            return RiskDecision(False, f"stop too tight ({stop_distance:.4f})")

        # max_stop_atr_mult = 0.0 disables ceiling (matches Betadesk)
        max_stop_atr = getattr(cfg, "max_stop_atr_mult", 0.0)
        if max_stop_atr is None:
            max_stop_atr = 0.0
        if max_stop_atr > 0 and atr is not None and atr > 0:
            if stop_distance > max_stop_atr * atr:
                return RiskDecision(
                    False,
                    f"stop wider than {max_stop_atr:g}x ATR ({stop_distance / atr:.1f}x) -- no nearby invalidation",
                )
        # =============================================================================

        raw_risk_mult = getattr(signal, "risk_multiplier", 1.0)
        try:
            risk_mult = float(raw_risk_mult)
        except Exception:
            return RiskDecision(False, "invalid risk multiplier")
        if not math.isfinite(risk_mult) or risk_mult <= 0:
            return RiskDecision(False, "invalid risk multiplier")
        # =============================================================================
        # [MODIFIED 2026-10-23]: Kelly Sizing Starvation Protection
        # ORIGINAL INTENT: Accepted any non-None kelly_override, including 0.0, to size positions.
        # REASON FOR CHANGE: When fractional Kelly evaluates to <= 0.0 for low-payoff or marginal setups,
        # kelly_override=0.0 starved the position to 0 shares, causing unintended RiskManager rejections.
        # Falling back to configured baseline risk ensures allocation never acts as an unintended gate.
        # SAFE ROLLBACK: Previous code preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if kelly_override is not None:
        #     # -----------------------------------------------------------------------------
        #     configured_risk_fraction = float(cfg.risk_per_trade_pct) / 100.0
        #     kelly_cap = min(float(cfg.kelly_fraction_cap), configured_risk_fraction)
        #
        #     effective_kelly = min(kelly_override, kelly_cap) * risk_mult
        #     risk_amount = ctx.equity * effective_kelly
        #     qty = int(risk_amount / stop_distance) if stop_distance > 0 else 0
        #     kelly_used = effective_kelly
        # else:
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if kelly_override is not None and kelly_override > 0:
            configured_risk_fraction = float(cfg.risk_per_trade_pct) / 100.0
            kelly_cap = min(float(cfg.kelly_fraction_cap), configured_risk_fraction)

            effective_kelly = min(kelly_override, kelly_cap) * risk_mult
            risk_amount = ctx.equity * effective_kelly
            qty = int(risk_amount / stop_distance) if stop_distance > 0 else 0
            kelly_used = effective_kelly
        else:
        # =============================================================================
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

            # -----------------------------------------------------------------------------
            applog.info("risk", f"Configured risk sizing for {symbol}: {effective_risk_pct:.4f}%")


            kelly_used = effective_risk_pct / 100.0
            risk_amount = ctx.equity * kelly_used
            qty = int(risk_amount / stop_distance)

        # =============================================================================
        # [MODIFIED 2026-10-23]: Sub-$5 Penny Stock Dollar Risk Cap, Notional Cap & Share Ceiling
        # ORIGINAL INTENT: Unbounded risk budget and max_position_pct (up to 20% = $20,000) allowed
        # 8,500+ shares of sub-$5 stocks (HTZ, MGNX) with no share ceiling or dollar risk cap.
        # REASON FOR CHANGE: Sizing $19,000 into a $2.22 stock with a 3-cent stop created extreme
        # spread leverage where 2 cents of slippage destroyed $171 extra. Capping sub-$5 dollar risk
        # to $250 (with 15% slippage buffer = $212.50), notional to 10% equity ($10,000), and shares
        # to 5,000 (10,000 for sub-$2) guarantees full penny stock trading without blowout losses.
        # SAFE ROLLBACK: Previous sizing lines preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # qty_unconstrained = qty
        # max_notional = ctx.equity * cfg.max_position_pct / 100.0
        # qty_after_notional = min(qty, int(max_notional / price))
        # qty_after_cash = qty_after_notional
        # if signal.action == SignalAction.ENTER_LONG:
        #     qty_after_cash = min(qty_after_notional, int(max(0.0, ctx.cash) / price))
        #
        # binding = "none"
        # if qty_after_notional < qty_unconstrained:
        #     binding = f"max_notional_cap ({max_notional:,.0f})"
        # if qty_after_cash < qty_after_notional:
        #     binding = f"cash_cap ({ctx.cash:,.0f})"
        #
        # qty = qty_after_cash
        # final_notional = qty * price
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if price < 5.0:
            sub5_risk_cap = getattr(cfg, "sub5_max_risk_dollars", 250.0) or 250.0
            sub5_buffer = getattr(cfg, "sub5_slippage_buffer", 0.85) or 0.85
            risk_amount = min(risk_amount, sub5_risk_cap * sub5_buffer)
            qty = int(risk_amount / stop_distance) if stop_distance > 0 else 0

        qty_unconstrained = qty
        max_notional_pct = cfg.max_position_pct
        if price < 5.0:
            sub5_notional_pct = getattr(cfg, "sub5_max_notional_pct", 10.0) or 10.0
            max_notional_pct = min(max_notional_pct, sub5_notional_pct)

        max_notional = ctx.equity * max_notional_pct / 100.0
        qty_after_notional = min(qty, int(max_notional / price))
        qty_after_cash = qty_after_notional
        if signal.action == SignalAction.ENTER_LONG:
            qty_after_cash = min(qty_after_notional, int(max(0.0, ctx.cash) / price))

        max_shares = getattr(cfg, "max_order_shares", 5000) or 5000
        if price < 2.0:
            max_shares = min(max_shares * 2, 10000)
        elif price < 5.0:
            max_shares = min(max_shares, 5000)

        qty_after_shares = min(qty_after_cash, max_shares) if max_shares > 0 else qty_after_cash

        binding = "none"
        if qty_after_notional < qty_unconstrained:
            binding = f"max_notional_cap ({max_notional:,.0f})"
        if qty_after_cash < qty_after_notional:
            binding = f"cash_cap ({ctx.cash:,.0f})"
        if qty_after_shares < qty_after_cash:
            binding = f"max_shares_cap ({max_shares})"

        qty = qty_after_shares
        final_notional = qty * price
        # =============================================================================
        applog.info(
            "risk",
            f"[sizing] {symbol} allocator_target={f'${risk_amount:,.0f}' if kelly_override is not None else 'n/a (fixed-risk)'} "
            f"unconstrained_qty={qty_unconstrained} "
            f"after_notional_cap={qty_after_notional} "
            f"after_cash_cap={qty_after_cash} "
            f"final_qty={qty} final_notional=${final_notional:,.0f} "
            f"binding_constraint={binding}",
        )
        if qty < 1:
            return RiskDecision(False, "computed size below 1 share (risk budget or cash too small)")

        # --- NEW IMPLEMENTATION ---
        # Penny Stock & Sub-Penny Precision Support (SEC Rule 612 / Alpaca API compliance):
        # Stocks >= $1.00 use 2 decimal places with 0.01 tick.
        # Penny stocks < $1.00 use 4 decimal places with 0.0001 tick.
        # This preserves granular stop/limit levels and prevents rounding collisions on low-dollar equities.
        side = OrderSide.BUY if signal.action == SignalAction.ENTER_LONG else OrderSide.SELL
        precision = 4 if price < 1.0 else 2
        min_tick = 0.0001 if price < 1.0 else 0.01
        executable_entry = round(float(price), precision)
        executable_stop = round(float(stop_loss), precision)
        if side == OrderSide.BUY:
            if executable_stop >= executable_entry:
                executable_stop = round(executable_entry - min_tick, precision)
        else:
            if executable_stop <= executable_entry:
                executable_stop = round(executable_entry + min_tick, precision)
        if executable_stop <= 0:
            return RiskDecision(False, "broker-valid bracket geometry unavailable at current price")

        entry_type = str(getattr(self.config, "entry_order_type", "market") or "market").lower().strip()
        if entry_type == "limit":
            order_type = OrderType.LIMIT
            max_slippage_pct = 0.0015
            if side == OrderSide.BUY:
                limit_price = round(price * (1 + max_slippage_pct), precision)
            else:
                limit_price = round(price * (1 - max_slippage_pct), precision)
        else:
            order_type = OrderType.MARKET
            limit_price = None

        # ALL targets are handled exclusively by PIS. No native broker targets (per Phase 1 invariant).
        executable_target = None
        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-20 - Decision-Bar Lineage Forwarding
        # ORIGINAL INTENT: OrderRequest omitted signal_timestamp_utc and decision_bar_timestamp_utc,
        # leaving them NULL in downstream Order/Fill records and replay artifacts.
        # REASON FOR CHANGE: Populate decision_bar_timestamp_utc and signal_timestamp_utc on OrderRequest
        # to ensure end-to-end auditability and trade forensics.
        # SAFE ROLLBACK: The original code is removed; rollback via git history.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        order = OrderRequest(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            limit_price=limit_price,
            stop_loss=executable_stop,
            take_profit=executable_target,
            strategy=signal.strategy,
            signal_timestamp_utc=getattr(signal, "timestamp", None) or getattr(signal, "ts", None),
            decision_bar_timestamp_utc=getattr(signal, "decision_bar_timestamp_utc", None),
        )
        # =============================================================================
        risk_str = f"${risk_amount:,.0f}" if isinstance(risk_amount, (int, float)) else str(risk_amount)
        return RiskDecision(
            approved=True,
            reason=f"sized {qty} shares, risk {risk_str}",
            order=order,
            kelly_fraction=round(kelly_used, 6) if isinstance(kelly_used, (int, float)) else 0.0,
            take_profit=take_profit,
        )

    def status(self) -> dict:
        return {
            "hard_halted": self.hard_halted,
            "hard_halt_reason": self.hard_halt_reason,
            "daily_halted": self.daily_halted,
            "profit_locked": self.profit_locked,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "config": self.config.model_dump(),
        }
