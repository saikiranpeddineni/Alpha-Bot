"""Risk manager -- every order passes through here before reaching a broker.

Capital preservation rules, in evaluation order:

1. kill switches: hard halt (max drawdown) and daily halt (max daily loss)
2. global anti-churn wait and thesis-aware re-entry protection
3. session windows: entries blocked near the close; flatten window enforced
4. exposure: max open positions, one position per symbol, per-position cap
5. instrument quality: minimum price and liquidity
6. behavioral selectivity: same-thesis re-entry and extreme mover chase
7. PDT protection for small live accounts
8. ATR/stop-based position sizing -- risk a fixed % of equity per trade

Historical losses are evidence, not a ticker blacklist. A losing trade can be a
bad entry, a good entry with a bad exit, or a valid opportunity that simply did
not work. The risk gate therefore blocks repeated *same-thesis* churn, not a
symbol merely because it previously lost money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import math

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
from app.core.applog import applog

PDT_EQUITY_MIN = 25_000.0
PDT_MAX_DAY_TRADES = 3  # legacy safety note; broker/account rules remain authoritative

# Beast Mode defaults. These stay inside the existing risk gate; no new
# decision subsystem is introduced.
BEAST_GLOBAL_COOLDOWN_MINUTES = 10
BEAST_SAME_THESIS_COOLDOWN_MINUTES = 120
BEAST_BEHAVIORAL_WINDOW_DAYS = 7
BEAST_BEHAVIORAL_LOSS_LIMIT = 3
BEAST_MIN_ENTRY_QUALITY = 82.0
BEAST_EXTREME_MOVER_PCT = 8.0
BEAST_EXTREME_MOVER_MIN_ATR_EXTENSION = 1.0
BEAST_EXTREME_MOVER_MIN_VWAP_DISTANCE_PCT = 1.50


def _beast_value(config: RiskConfig, name: str, default):
    return getattr(config, name, default)


def _bucket(value: float | None, step: float) -> int | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return int(round(value / step))


def ensure_thesis_fingerprint(signal: Signal) -> str:
    """Create a stable thesis fingerprint from decision-time evidence."""
    existing = getattr(signal, "thesis_fingerprint", None)
    if existing:
        return existing

    params = signal.strategy_params or {}
    regime = getattr(getattr(signal, "regime", None), "value", None)
    if regime is None:
        regime = str(getattr(signal, "regime", "")) or None

    payload = {
        "symbol": signal.symbol.upper(),
        "action": getattr(signal.action, "value", str(signal.action)),
        "strategy": signal.strategy or "",
        "regime": regime,
        "opportunity_state": getattr(signal, "opportunity_state", None) or params.get("opportunity_state"),
        "mover_bias": getattr(signal, "mover_bias", None),
        "news_direction": getattr(signal, "news_direction", "neutral"),
        "event_type": getattr(signal, "event_type", None),
        "price_discovery": bool(getattr(signal, "price_discovery", False)),
        "entry_quality": getattr(signal, "entry_quality", None) or params.get("entry_quality"),
        "entry_quality_bucket": _bucket(getattr(signal, "entry_quality_score", None) or params.get("entry_quality_score"), 5.0),
        "trend_bucket": _bucket(getattr(signal, "trend_score", None) or params.get("trend_score"), 10.0),
        "location_bucket": _bucket(getattr(signal, "location_score", None) or params.get("location_score"), 10.0),
        "extension_bucket": _bucket(getattr(signal, "atr_extension", None) or params.get("atr_extension"), 0.50),
        "vwap_bucket": _bucket(getattr(signal, "vwap_distance_pct", None) or params.get("vwap_distance_pct"), 0.50),
        "catalyst_bucket": _bucket(getattr(signal, "catalyst_score", None) or params.get("catalyst_score"), 10.0),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
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
        # Retained for compatibility/status; no longer used as historical-loss vetoes.
        self.strategy_consecutive_losses: dict[str, int] = {}
        self.strategy_cooldowns: dict[str, datetime] = {}
        self.symbol_cooldowns: dict[str, datetime] = {}
        self.behavioral_loop_cooldowns: dict[str, datetime] = {}
        # Historical loss context is informational and can be used by future
        # entry scoring; it never directly blocks a fresh opportunity.
        self.historical_loss_context: dict[str, dict] = {}
        self._pdt_warned = False
        self.core_watchlist: list[str] | None = None
        self.closed_theses: dict[str, datetime] = {}

    def get_risk_state(self) -> dict:
        return {
            "consecutive_losses": self.consecutive_losses,
            "strategy_consecutive_losses": dict(self.strategy_consecutive_losses),
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "global_cooldown_until": self.global_cooldown_until.isoformat() if self.global_cooldown_until else None,
            # These fields are retained for backward-compatible checkpoint shape,
            # but fresh historical loss blocks are never restored from them.
            "symbol_cooldowns": {sym: ts.isoformat() for sym, ts in self.symbol_cooldowns.items()},
            "strategy_cooldowns": {strat: ts.isoformat() for strat, ts in self.strategy_cooldowns.items()},
            "behavioral_loop_cooldowns": {sym: ts.isoformat() for sym, ts in self.behavioral_loop_cooldowns.items()},
            "historical_loss_context": dict(self.historical_loss_context),
            "hard_halted": self.hard_halted,
            "hard_halt_reason": self.hard_halt_reason,
            "daily_halted": self.daily_halted,
            "daily_halt_date": self.daily_halt_date.isoformat() if self.daily_halt_date else None,
            "closed_theses": {tf: ts.isoformat() for tf, ts in self.closed_theses.items()},
        }

    def restore_risk_state(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        self.consecutive_losses = data.get("consecutive_losses", 0)
        self.strategy_consecutive_losses = data.get("strategy_consecutive_losses", {})
        if data.get("cooldown_until"):
            try:
                self.cooldown_until = datetime.fromisoformat(data["cooldown_until"])
            except Exception:
                pass
        if data.get("global_cooldown_until"):
            try:
                self.global_cooldown_until = datetime.fromisoformat(data["global_cooldown_until"])
            except Exception:
                pass
        if data.get("daily_halt_date"):
            try:
                self.daily_halt_date = date.fromisoformat(data["daily_halt_date"])
            except Exception:
                pass
        # Deliberately do NOT restore symbol/behavioral cooldowns from old
        # checkpoints: those fields represented the retired symbol blacklist.
        self.symbol_cooldowns.clear()
        self.behavioral_loop_cooldowns.clear()
        self.strategy_cooldowns.clear()
        self.historical_loss_context = data.get("historical_loss_context", {}) or {}
        self.hard_halted = bool(data.get("hard_halted", False))
        self.hard_halt_reason = str(data.get("hard_halt_reason", ""))
        self.daily_halted = bool(data.get("daily_halted", False))
        if data.get("closed_theses"):
            for tf, ts_str in data["closed_theses"].items():
                try:
                    self.closed_theses[tf] = datetime.fromisoformat(ts_str)
                except Exception:
                    pass

    def reconstruct_cooldowns(self, trades: list[TradeRecord], now: datetime | None = None) -> None:
        """Rebuild useful loss/thesis memory without blacklisting symbols or strategies."""
        if now is None:
            now = datetime.now(UTC)
        cfg = self.config
        global_minutes = int(_beast_value(cfg, "beast_global_cooldown_minutes", BEAST_GLOBAL_COOLDOWN_MINUTES))
        same_thesis_minutes = int(_beast_value(cfg, "beast_same_thesis_cooldown_minutes", BEAST_SAME_THESIS_COOLDOWN_MINUTES))
        behavioral_days = int(_beast_value(cfg, "beast_behavioral_window_days", BEAST_BEHAVIORAL_WINDOW_DAYS))

        completed = [t for t in trades if getattr(t, "exit_at", None)]
        completed.sort(key=lambda t: t.exit_at, reverse=True)
        if completed and global_minutes > 0:
            cooldown_end = completed[0].exit_at + timedelta(minutes=global_minutes)
            if cooldown_end > now:
                self.global_cooldown_until = cooldown_end

        recent_cutoff = now - timedelta(days=behavioral_days)
        by_symbol: dict[str, list[TradeRecord]] = {}
        for trade in completed:
            symbol = trade.symbol.upper()
            if trade.exit_at >= recent_cutoff and trade.pnl <= 0:
                by_symbol.setdefault(symbol, []).append(trade)

            fingerprint = getattr(trade, "thesis_fingerprint", None)
            if fingerprint and trade.pnl <= 0 and same_thesis_minutes > 0:
                block_until = trade.exit_at + timedelta(minutes=same_thesis_minutes)
                if block_until > now:
                    self.closed_theses[fingerprint] = max(
                        self.closed_theses.get(fingerprint, trade.exit_at), trade.exit_at
                    )

        self.historical_loss_context = {}
        for symbol, history in by_symbol.items():
            history.sort(key=lambda t: t.exit_at, reverse=True)
            self.historical_loss_context[symbol] = {
                "recent_loss_count": len(history),
                "last_loss_at": history[0].exit_at.isoformat() if history else None,
                "behavioral_warning": len(history) >= BEAST_BEHAVIORAL_LOSS_LIMIT,
            }
            if len(history) >= BEAST_BEHAVIORAL_LOSS_LIMIT:
                applog.warning(
                    "risk",
                    f"BEAST CONTEXT: {symbol} has {len(history)} losses in the last {behavioral_days}d; "
                    "retaining as caution context, NOT blocking the ticker",
                )

        self.consecutive_losses = 0
        self.strategy_consecutive_losses = {}
        self.strategy_cooldowns.clear()
        self.symbol_cooldowns.clear()
        self.behavioral_loop_cooldowns.clear()
        active = [self.global_cooldown_until] if self.global_cooldown_until and self.global_cooldown_until > now else []
        self.cooldown_until = max(active) if active else None
        applog.info("engine", f"P0-3: Reconstructed loss/thesis context from {len(completed)} recent trades; no symbol loss blacklists restored.")

    # ---- lifecycle hooks ----

    def on_trade_closed(self, trade: TradeRecord, now: datetime | None = None) -> None:
        strategy = trade.strategy or "manual"
        now_dt = now or datetime.now(UTC)
        global_minutes = int(_beast_value(self.config, "beast_global_cooldown_minutes", BEAST_GLOBAL_COOLDOWN_MINUTES))
        same_thesis_minutes = int(_beast_value(self.config, "beast_same_thesis_cooldown_minutes", BEAST_SAME_THESIS_COOLDOWN_MINUTES))

        if global_minutes > 0:
            self.global_cooldown_until = now_dt + timedelta(minutes=global_minutes)

        if trade.pnl <= 0:
            symbol = trade.symbol.upper()
            ctx = self.historical_loss_context.setdefault(symbol, {"recent_loss_count": 0, "last_loss_at": None, "behavioral_warning": False})
            ctx["recent_loss_count"] = int(ctx.get("recent_loss_count", 0)) + 1
            ctx["last_loss_at"] = now_dt.isoformat()
            ctx["behavioral_warning"] = ctx["recent_loss_count"] >= BEAST_BEHAVIORAL_LOSS_LIMIT
            applog.warning(
                "risk",
                f"BEAST CONTEXT: loss on {symbol} recorded for learning; ticker remains eligible for fresh theses"
            )

            fingerprint = getattr(trade, "thesis_fingerprint", None)
            if fingerprint and same_thesis_minutes > 0:
                self.closed_theses[fingerprint] = now_dt
                applog.info("risk", f"Registered losing thesis fingerprint {fingerprint}; same-thesis re-entry waits {same_thesis_minutes}m")

            self.strategy_consecutive_losses[strategy] = self.strategy_consecutive_losses.get(strategy, 0) + 1
        else:
            self.strategy_consecutive_losses[strategy] = 0

        active = [
            t for t in ([self.global_cooldown_until] if self.global_cooldown_until else [])
            if t > now_dt
        ]
        self.consecutive_losses = max(self.strategy_consecutive_losses.values(), default=0)
        self.cooldown_until = max(active) if active else None

    def roll_session(self, session_date) -> None:  # noqa: ANN001
        now = datetime.now(UTC)
        previous_session = getattr(self, "_manager_session_date", None)
        if previous_session is None:
            self._manager_session_date = session_date
            self.symbol_cooldowns.clear()
            self.behavioral_loop_cooldowns.clear()
            self.strategy_cooldowns.clear()
            same_thesis_minutes = int(_beast_value(self.config, "beast_same_thesis_cooldown_minutes", BEAST_SAME_THESIS_COOLDOWN_MINUTES))
            self.closed_theses = {k: v for k, v in self.closed_theses.items() if v + timedelta(minutes=same_thesis_minutes) > now}
            self.cooldown_until = self.global_cooldown_until if self.global_cooldown_until and self.global_cooldown_until > now else None
            if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
                self.daily_halted = False
                self.daily_halt_date = None
                applog.info("risk", "New session -- daily loss halt cleared")
            return
        if session_date <= previous_session:
            return
        self._manager_session_date = session_date
        if self.daily_halted and self.daily_halt_date is not None and session_date > self.daily_halt_date:
            self.daily_halted = False
            self.daily_halt_date = None
            applog.info("risk", "New session -- daily loss halt cleared")
        self.consecutive_losses = 0
        self.strategy_consecutive_losses.clear()
        self.symbol_cooldowns.clear()
        self.strategy_cooldowns.clear()
        self.behavioral_loop_cooldowns.clear()
        same_thesis_minutes = int(_beast_value(self.config, "beast_same_thesis_cooldown_minutes", BEAST_SAME_THESIS_COOLDOWN_MINUTES))
        self.closed_theses = {k: v for k, v in self.closed_theses.items() if v + timedelta(minutes=same_thesis_minutes) > now}
        self.cooldown_until = self.global_cooldown_until if self.global_cooldown_until and self.global_cooldown_until > now else None

    def reset_hard_halt(self) -> None:
        self.hard_halted = False
        self.hard_halt_reason = ""
        applog.warning("risk", "Hard halt manually reset -- trading re-enabled")

    def check_halts(self, ctx: AccountContext, now: datetime | None = None) -> str | None:
        if not self.hard_halted and ctx.peak_equity > 0:
            dd_pct = (ctx.peak_equity - ctx.equity) / ctx.peak_equity * 100.0
            if dd_pct >= self.config.max_drawdown_pct:
                self.hard_halted = True
                self.hard_halt_reason = f"Max drawdown breached: {dd_pct:.1f}% >= {self.config.max_drawdown_pct}%"
                applog.error("risk", f"KILL SWITCH: {self.hard_halt_reason}")
                return "hard"
        if not self.daily_halted and ctx.session_start_equity > 0:
            loss_pct = -ctx.day_pnl / ctx.session_start_equity * 100.0
            if loss_pct >= self.config.max_daily_loss_pct:
                self.daily_halted = True
                self.daily_halt_date = self.clock.session_date(now)
                applog.error("risk", f"DAILY HALT: day loss {loss_pct:.2f}% >= {self.config.max_daily_loss_pct}% -- flattening and stopping for the session")
                return "daily"
        return None

    def in_cooldown(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.cooldown_until is not None and now < self.cooldown_until

    def is_strategy_in_cooldown(self, strategy: str, now: datetime | None = None) -> bool:
        # Historical strategy losses are telemetry/context, not a hard veto.
        return False

    def should_flatten(self, now: datetime | None = None) -> bool:
        return self.clock.in_flatten_window(self.config.flatten_minutes_before_close, now)

    def _entry_quality_value(self, signal: Signal) -> float | None:
        params = signal.strategy_params or {}
        value = getattr(signal, "entry_quality_score", None)
        if value is None:
            value = params.get("entry_quality_score")
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return value if value is not None and math.isfinite(value) else None

    def _entry_extension_value(self, signal: Signal, key: str) -> float | None:
        params = signal.strategy_params or {}
        value = getattr(signal, key, None)
        if value is None:
            value = params.get(key)
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return value if value is not None and math.isfinite(value) else None

    def _beast_entry_gate(self, signal: Signal, now: datetime) -> RiskDecision | None:
        """Selectivity gate: block churned theses, not historically losing tickers."""
        cfg = self.config
        strategy = (signal.strategy or "").lower()
        ensure_thesis_fingerprint(signal)

        if self.global_cooldown_until and now < self.global_cooldown_until:
            remaining = max(0.0, (self.global_cooldown_until - now).total_seconds() / 60.0)
            return RiskDecision(False, f"BEAST_WAIT: global trade cooldown {remaining:.1f}m remaining")

        fingerprint = getattr(signal, "thesis_fingerprint", None)
        same_thesis_minutes = int(_beast_value(cfg, "beast_same_thesis_cooldown_minutes", BEAST_SAME_THESIS_COOLDOWN_MINUTES))
        if fingerprint:
            closed_at = self.closed_theses.get(fingerprint)
            if closed_at and now < closed_at + timedelta(minutes=same_thesis_minutes):
                remaining = max(0.0, ((closed_at + timedelta(minutes=same_thesis_minutes)) - now).total_seconds() / 60.0)
                return RiskDecision(False, f"BEAST_WAIT: same thesis lost recently; {remaining:.1f}m remaining")

        eq = self._entry_quality_value(signal)
        min_eq = float(_beast_value(cfg, "beast_min_entry_quality", BEAST_MIN_ENTRY_QUALITY))
        if strategy == "momentum" and eq is not None and eq < min_eq:
            return RiskDecision(False, f"BEAST_WAIT: entry quality {eq:.1f} < beast threshold {min_eq:.1f}")

        atr_extension = self._entry_extension_value(signal, "atr_extension")
        vwap_distance = self._entry_extension_value(signal, "vwap_distance_pct")
        mover_pct = float(getattr(signal, "mover_change_pct", 0.0) or 0.0)
        mover_bias = (getattr(signal, "mover_bias", None) or "").lower()
        news_direction = (getattr(signal, "news_direction", "neutral") or "neutral").lower()
        news_confidence = float(getattr(signal, "news_confidence", 0.0) or 0.0)
        price_discovery = bool(getattr(signal, "price_discovery", False))
        event_state = str(getattr(signal, "event_state", None) or getattr(signal, "opportunity_state", None) or "").upper()

        aligned_extreme_mover = (
            signal.action == SignalAction.ENTER_LONG and mover_bias == "gainer" and mover_pct >= float(_beast_value(cfg, "beast_extreme_mover_pct", BEAST_EXTREME_MOVER_PCT))
        ) or (
            signal.action == SignalAction.ENTER_SHORT and mover_bias == "loser" and mover_pct <= -float(_beast_value(cfg, "beast_extreme_mover_pct", BEAST_EXTREME_MOVER_PCT))
        )
        if aligned_extreme_mover:
            overextended = (
                atr_extension is not None and atr_extension >= float(_beast_value(cfg, "beast_extreme_mover_min_atr_extension", BEAST_EXTREME_MOVER_MIN_ATR_EXTENSION))
            ) or (
                vwap_distance is not None and abs(vwap_distance) >= float(_beast_value(cfg, "beast_extreme_mover_min_vwap_distance_pct", BEAST_EXTREME_MOVER_MIN_VWAP_DISTANCE_PCT))
            )
            strong_aligned_catalyst = news_confidence >= 0.70 and ((signal.action == SignalAction.ENTER_LONG and news_direction == "positive") or (signal.action == SignalAction.ENTER_SHORT and news_direction == "negative"))
            if overextended and not strong_aligned_catalyst and not price_discovery and "EVENT" not in event_state:
                return RiskDecision(False, f"BEAST_WAIT: extreme mover chase rejected ({mover_pct:+.1f}%, atr_ext={atr_extension}, vwap_dist={vwap_distance})")

        return None

    def is_entry_permitted(self, signal: Signal, now: datetime | None = None) -> RiskDecision:
        cfg = self.config
        strategy = signal.strategy or "manual"
        now = now or datetime.now(UTC)
        if self.hard_halted:
            return RiskDecision(False, f"hard halt active: {self.hard_halt_reason}")
        if self.daily_halted:
            return RiskDecision(False, "daily loss halt active")
        if not self.clock.in_entry_window(cfg.no_entry_minutes_before_close, now):
            return RiskDecision(False, "outside entry window (market closed or too near close)")
        beast_gate = self._beast_entry_gate(signal, now)
        if beast_gate is not None:
            return beast_gate
        return RiskDecision(True, "permitted")

    def evaluate_entry(self, signal: Signal, ctx: AccountContext, bars: pd.DataFrame, now: datetime | None = None, kelly_override: float | None = None) -> RiskDecision:
        pre_check = self.is_entry_permitted(signal, now)
        if not pre_check.approved:
            return pre_check
        cfg = self.config
        symbol = signal.symbol.upper()
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
        if ctx.rolling_day_trades >= PDT_MAX_DAY_TRADES and ctx.equity < PDT_EQUITY_MIN and not ctx.is_paper:
            return RiskDecision(False, "PDT guard: day-trade limit reached for live account")
        if ctx.rolling_day_trades >= PDT_MAX_DAY_TRADES and ctx.equity < PDT_EQUITY_MIN and ctx.is_paper and not self._pdt_warned:
            self._pdt_warned = True
            applog.warning("risk", "PDT note: this pace of day trading would exceed the pattern-day-trader limit on a live account under $25k")

        stop_loss = signal.stop_loss
        take_profit = signal.take_profit
        atr = float(bars["atr14"].iloc[-1]) if "atr14" in bars.columns and pd.notna(bars["atr14"].iloc[-1]) else None
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

        stop_distance = abs(price - stop_loss)
        min_distance = max(price * 0.0005, 0.01)
        if stop_distance < min_distance:
            return RiskDecision(False, f"stop too tight ({stop_distance:.4f})")

        risk_mult = getattr(signal, "risk_multiplier", 1.0)
        if kelly_override is not None:
            base_risk_pct_cap = (getattr(cfg, "risk_per_trade_pct", 1.0) * 2.0) / 100.0
            kelly_cap = min(getattr(cfg, "kelly_fraction_cap", 0.15), base_risk_pct_cap)
            effective_kelly = min(kelly_override, kelly_cap) * risk_mult
            risk_amount = ctx.equity * effective_kelly
            qty = int(risk_amount / stop_distance) if stop_distance > 0 else 0
            kelly_used = effective_kelly
        else:
            effective_risk_pct = cfg.risk_per_trade_pct
            explicit_scanner = getattr(signal, "discovery_source", None) == "scanner"
            legacy_dynamic = not explicit_scanner and getattr(signal, "discovery_source", None) is None and self.core_watchlist is not None and symbol not in self.core_watchlist
            if explicit_scanner:
                scanner_score = getattr(signal, "scanner_score", None)
                scanner_auc = getattr(signal, "scanner_auc", None)
                if scanner_score is not None and scanner_auc is not None:
                    scanner_mult = 1.00 if scanner_score >= 0.85 and scanner_auc >= 0.60 else 0.75 if scanner_score >= 0.70 and scanner_auc >= 0.55 else 0.50
                else:
                    scanner_mult = 0.50
                effective_risk_pct *= scanner_mult
                applog.info("risk", f"Explicit scanner asset {symbol}: scaling risk by {scanner_mult:.2f} to {effective_risk_pct:.4f}%")
            elif legacy_dynamic:
                effective_risk_pct *= 0.75
                applog.info("risk", f"Legacy dynamic asset {symbol}: scaling risk by 0.75 to {effective_risk_pct:.4f}%")
            effective_risk_pct *= risk_mult
            conf = getattr(signal, "score", None)
            if isinstance(conf, (int, float)) and conf > 0.0:
                clamped_conf = max(0.50, min(0.90, conf))
                ratio = (clamped_conf - 0.50) / 0.40
                base_risk = 0.50 + ratio * 1.50
                effective_risk_pct = base_risk * risk_mult
                applog.info("risk", f"Dynamic sizing: TradeScore {conf:.2f} -> scaled risk to {effective_risk_pct:.2f}%")
            else:
                applog.info("risk", f"No TradeScore for {symbol}: using base risk {effective_risk_pct}%")
            kelly_used = effective_risk_pct / 100.0
            risk_amount = ctx.equity * kelly_used
            qty = int(risk_amount / stop_distance)

        current_heat_amount = 0.0
        for p in ctx.positions:
            current_heat_amount += abs(p.avg_entry_price - p.stop_loss) * abs(p.qty) if p.stop_loss is not None else abs(p.market_value) * 0.02
        new_heat_pct = (current_heat_amount + risk_amount) / max(ctx.equity, 1.0)
        if new_heat_pct > 0.05:
            return RiskDecision(False, f"Max portfolio heat exceeded ({new_heat_pct*100:.1f}% > 5.0%)")

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
        applog.info("risk", f"[sizing] {symbol} unconstrained_qty={qty_unconstrained} after_notional_cap={qty_after_notional} after_cash_cap={qty_after_cash} final_qty={qty} final_notional=${final_notional:,.0f} binding_constraint={binding}")
        if qty < 1:
            return RiskDecision(False, "computed size below 1 share (risk budget or cash too small)")

        side = OrderSide.BUY if signal.action == SignalAction.ENTER_LONG else OrderSide.SELL
        executable_entry = round(float(price), 2)
        executable_stop = round(float(stop_loss), 2)
        if side == OrderSide.BUY and executable_stop >= executable_entry:
            executable_stop = round(executable_entry - 0.01, 2)
        if side == OrderSide.SELL and executable_stop <= executable_entry:
            executable_stop = round(executable_entry + 0.01, 2)
        if executable_stop <= 0:
            return RiskDecision(False, "broker-valid bracket geometry unavailable at current price")

        entry_type = str(getattr(self.config, "entry_order_type", "market") or "market").lower().strip()
        if entry_type == "limit":
            order_type = OrderType.LIMIT
            limit_price = round(price * (1 + 0.0015), 2) if side == OrderSide.BUY else round(price * (1 - 0.0015), 2)
        else:
            order_type = OrderType.MARKET
            limit_price = None

        params = signal.strategy_params or {}
        order = OrderRequest(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            limit_price=limit_price,
            stop_loss=executable_stop,
            take_profit=None,
            strategy=signal.strategy,
            reason=signal.reason,
            thesis_fingerprint=getattr(signal, "thesis_fingerprint", None),
            signal_timestamp_utc=getattr(signal, "ts", None),
            decision_bar_timestamp_utc=getattr(signal, "decision_bar_timestamp_utc", None),
            signal_generated_timestamp_utc=getattr(signal, "signal_generated_timestamp_utc", None),
            decision_completed_timestamp_utc=getattr(signal, "decision_completed_timestamp_utc", None),
            entry_quality_score=self._entry_quality_value(signal),
            entry_quality=getattr(signal, "entry_quality", None) or params.get("entry_quality"),
            opportunity_state=getattr(signal, "opportunity_state", None) or params.get("opportunity_state"),
            opportunity_score=getattr(signal, "opportunity_score", None) or params.get("opportunity_score"),
            vwap_distance_pct=self._entry_extension_value(signal, "vwap_distance_pct"),
            atr_extension=self._entry_extension_value(signal, "atr_extension"),
            pre_entry_impulse_pct=self._entry_extension_value(signal, "pre_entry_impulse_pct"),
            event_risk=getattr(signal, "event_risk", "normal"),
            event_type=getattr(signal, "event_type", None),
            event_age_minutes=getattr(signal, "event_age_minutes", None),
            price_discovery=bool(getattr(signal, "price_discovery", False)),
            reentry_attempt=int(getattr(signal, "reentry_attempt", 0) or 0),
            same_thesis_blocked=bool(getattr(signal, "same_thesis_blocked", False)),
            trend_score=getattr(signal, "trend_score", None) or params.get("trend_score"),
            location_score=getattr(signal, "location_score", None) or params.get("location_score"),
            extension_score=getattr(signal, "extension_score", None) or params.get("extension_score"),
            structure_score=getattr(signal, "structure_score", None) or params.get("structure_score"),
            metadata={
                "candidate_id": getattr(signal, "candidate_id", None),
                "thesis_fingerprint": getattr(signal, "thesis_fingerprint", None),
                "entry_quality_score": self._entry_quality_value(signal),
                "entry_quality": getattr(signal, "entry_quality", None) or params.get("entry_quality"),
                "opportunity_state": getattr(signal, "opportunity_state", None) or params.get("opportunity_state"),
                "opportunity_score": getattr(signal, "opportunity_score", None) or params.get("opportunity_score"),
                "vwap_distance_pct": self._entry_extension_value(signal, "vwap_distance_pct"),
                "atr_extension": self._entry_extension_value(signal, "atr_extension"),
                "pre_entry_impulse_pct": self._entry_extension_value(signal, "pre_entry_impulse_pct"),
                "mover_bias": getattr(signal, "mover_bias", None),
                "mover_change_pct": float(getattr(signal, "mover_change_pct", 0.0) or 0.0),
                "rs_score": getattr(signal, "rs_score", None),
                "news_direction": getattr(signal, "news_direction", "neutral"),
                "news_confidence": float(getattr(signal, "news_confidence", 0.0) or 0.0),
                "historical_loss_context": self.historical_loss_context.get(symbol),
            },
        )
        risk_str = f"${risk_amount:,.0f}"
        return RiskDecision(True, f"sized {qty} shares, risk {risk_str}", order=order, kelly_fraction=round(kelly_used, 6) if isinstance(kelly_used, (int, float)) else 0.0)

    def status(self) -> dict:
        return {
            "hard_halted": self.hard_halted,
            "hard_halt_reason": self.hard_halt_reason,
            "daily_halted": self.daily_halted,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "global_cooldown_until": self.global_cooldown_until.isoformat() if self.global_cooldown_until else None,
            "behavioral_loop_cooldowns": {},
            "historical_loss_context": dict(self.historical_loss_context),
            "config": self.config.model_dump(),
        }
