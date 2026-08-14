"""Portfolio tracker.

Consumes broker fills, maintains an entry ledger, emits closed TradeRecords,
and tracks the equity curve plus session statistics (day P&L, peak equity,
day-trade count). Broker-agnostic: works identically for the paper simulator,
Alpaca, and the backtester.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, NamedTuple

from app.brokers.base import Fill
from app.core.domain import OrderSide, TradeRecord, ExecutionTelemetry
from app.core.applog import applog


class RollResult(NamedTuple):
    """Return value of :meth:`PortfolioTracker.roll_session`.

    Backwards-compatible with the previous ``bool`` return: callers that
    do ``if portfolio.roll_session(...)`` continue to work because
    ``bool(RollResult(rolled=True, ...))`` is ``True`` and
    ``bool(RollResult(rolled=False, ...))`` is ``False``.
    """

    rolled: bool
    """True when the session actually advanced to a new date."""
    open_symbols: tuple[str, ...] = ()
    """Symbols with open lots at the session boundary (empty when rolled=False)."""

    def __bool__(self) -> bool:  # noqa: D105
        return self.rolled


# ---------------------------------------------------------------------------
# PDT rolling-window helpers
# ---------------------------------------------------------------------------
_PDT_WINDOW_BUSINESS_DAYS = 5


def _business_days_ago(reference: date, n: int) -> date:
    """Return the date that is *n* business days (Mon-Fri) before *reference*.

    This is a simple calendar walk; it does not account for market holidays
    because the SEC PDT rule counts calendar business days, not exchange
    trading days.  Using exchange-holiday awareness here would be over-
    engineering and potentially unsafe (wrong direction).
    """
    remaining = n
    cursor = reference
    while remaining > 0:
        cursor = date.fromordinal(cursor.toordinal() - 1)
        if cursor.weekday() < 5:  # Mon=0 … Fri=4
            remaining -= 1
    return cursor


@dataclass
class _OpenLot:
    qty: float  # signed: + long, - short
    avg_price: float
    entry_ts: datetime
    strategy: str
    stop_loss: float | None = None
    initial_stop_loss: float | None = None
    initial_risk_per_share: float | None = None
    initial_risk_dollars: float = 0.0
    commission_basis: float = 0.0
    closing_qty: float = 0.0
    telemetry: ExecutionTelemetry = field(default_factory=ExecutionTelemetry)
    closing_notional: float = 0.0
    realized_pnl_accum: float = 0.0
    candidate_id: str | None = None
    thesis_fingerprint: str | None = None
    signal_generated_timestamp_utc: datetime | None = None
    decision_completed_timestamp_utc: datetime | None = None
    entry_quality_score: float | None = None
    entry_quality: str | None = None
    opportunity_state: str | None = None
    opportunity_score: float | None = None
    vwap_distance_pct: float | None = None
    atr_extension: float | None = None
    pre_entry_impulse_pct: float | None = None
    event_risk: str = "normal"
    event_type: str | None = None
    event_age_minutes: float | None = None
    price_discovery: bool = False
    reentry_attempt: int = 0
    same_thesis_blocked: bool = False
    trend_score: float | None = None
    location_score: float | None = None
    extension_score: float | None = None
    structure_score: float | None = None


@dataclass
class FillApplicationResult:
    realized_pnl_fragment: float = 0.0
    closed_trade: TradeRecord | None = None
    position_closed: bool = False

    @property
    def pnl(self) -> float:
        return self.closed_trade.pnl if self.closed_trade is not None else self.realized_pnl_fragment

    @property
    def qty(self) -> float:
        return self.closed_trade.qty if self.closed_trade is not None else 0.0


@dataclass
class PortfolioTracker:
    max_curve_points: int = 5000
    restore_mode: str | None = None
    # fix(#61): bind checkpoint restoration to a specific broker account_id
    restore_account_id: str | None = None
    realized_pnl: float = 0.0
    day_realized_pnl: float = 0.0
    session_date: date | None = None
    session_start_equity: float | None = None
    peak_equity: float = 0.0
    # Rolling log of dates on which an intraday round-trip was completed.
    # Entries older than _PDT_WINDOW_BUSINESS_DAYS business days are pruned
    # during roll_session so the list stays bounded.
    _day_trade_dates: list[date] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    _lots: dict[str, _OpenLot] = field(default_factory=dict)
    _seen_fill_keys: set = field(default_factory=set)
    _cumulative_fills: dict[str, float] = field(default_factory=dict)
    _cumulative_notionals: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.restore_mode not in {"live", "paper"}:
            return
        from app.db.database import Database
        db = Database.active()
        if db is None:
            return
        checkpoint = db.get_portfolio_checkpoint(self.restore_mode)
        if checkpoint:
            stored_account_id = checkpoint.get("account_id")
            if self.restore_account_id is not None and stored_account_id != self.restore_account_id:
                applog.error(
                    "portfolio",
                    f"#61 CRITICAL: Refusing to restore {self.restore_mode} checkpoint: "
                    f"stored account_id={stored_account_id!r} does not match required account_id={self.restore_account_id!r}."
                )
                db.save_portfolio_checkpoint(self.restore_mode, self.checkpoint(account_id=self.restore_account_id))
                return

            self.restore_checkpoint(checkpoint)
            applog.info("portfolio", f"Restored durable {self.restore_mode} portfolio checkpoint (account_id={stored_account_id!r})")

    def day_trades_in_window(self, as_of: date | datetime | None = None) -> int:
        session = as_of or self.session_date or (
            self._day_trade_dates[-1] if self._day_trade_dates else date.today()
        )
        if isinstance(session, datetime):
            session = session.date()
        window_start = _business_days_ago(session, _PDT_WINDOW_BUSINESS_DAYS - 1)
        return sum(1 for d in self._day_trade_dates if window_start <= d <= session)

    @property
    def day_trades_today(self) -> int:
        ref = self.session_date or (self._day_trade_dates[-1] if self._day_trade_dates else date.today())
        return sum(1 for d in self._day_trade_dates if d == ref)

    @day_trades_today.setter
    def day_trades_today(self, value: int) -> None:
        ref = self.session_date or (self._day_trade_dates[-1] if self._day_trade_dates else date.today())
        other_dates = [d for d in self._day_trade_dates if d != ref]
        self._day_trade_dates = other_dates + [ref] * max(0, int(value))

    def roll_session(self, session: date, equity: float) -> RollResult:
        if self.session_date is None:
            self.session_date = session
            self.session_start_equity = equity
            self.peak_equity = max(self.peak_equity, equity)
            self.day_realized_pnl = 0.0
            window_start = _business_days_ago(session, _PDT_WINDOW_BUSINESS_DAYS)
            self._day_trade_dates = [d for d in self._day_trade_dates if d > window_start]
            carry = [s for s, lot in self._lots.items() if abs(lot.qty) >= 1e-9]
            return RollResult(rolled=True, open_symbols=tuple(carry))
        if session <= self.session_date:
            return RollResult(rolled=False)
        applog.info("portfolio", f"Session roll: {self.session_date} -> {session} (Equity: ${equity:,.2f})")
        carry = [s for s, lot in self._lots.items() if abs(lot.qty) >= 1e-9]
        if carry:
            applog.warning("portfolio", f"Overnight position carryover ({len(carry)}): carrying {', '.join(sorted(carry))}")
        self.session_date = session
        self.session_start_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        self.day_realized_pnl = 0.0
        window_start = _business_days_ago(session, _PDT_WINDOW_BUSINESS_DAYS - 1)
        self._day_trade_dates = [d for d in self._day_trade_dates if d >= window_start]
        return RollResult(rolled=True, open_symbols=tuple(carry))

    def mark_equity(self, equity: float, ts: datetime) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        curve = self.equity_curve
        if curve and curve[-1][0] == ts:
            curve[-1] = (ts, equity)
        else:
            curve.append((ts, equity))
        if len(curve) > self.max_curve_points:
            keep = curve[: len(curve) // 2 : 2] + curve[len(curve) // 2 :]
            self.equity_curve = keep

    def day_pnl(self, equity: float) -> float:
        if self.session_start_equity is None:
            return 0.0
        return equity - self.session_start_equity

    def drawdown_pct(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity * 100.0)

    @property
    def equity(self) -> float:
        if self.equity_curve:
            return self.equity_curve[-1][1]
        if self.session_start_equity is not None:
            return self.session_start_equity
        return 100000.0

    def note_stop(self, symbol: str, stop_loss: float | None) -> None:
        """Record a broker-confirmed stop while preserving immutable entry risk.

        Later trailing, breakeven, or runner ratchets update only
        the current stop and can never rewrite the original R denominator.
        """
        lot = self._lots.get(symbol.upper())
        if lot is None or stop_loss is None:
            return
        stop = float(stop_loss)
        lot.stop_loss = stop
        
        # P0-1: Initial risk MUST be established at the time of entry (in on_fill).
        # We explicitly DO NOT retroactively establish initial_risk_per_share here 
        # if it is missing, as that violates the immutable risk geometry invariant.

    def open_positions(self) -> dict[str, _OpenLot]:
        return {s: lot for s, lot in self._lots.items() if abs(lot.qty) >= 1e-9}

    def checkpoint(self, account_id: str | None = None, risk_state: dict | None = None) -> dict:
        return {
            "account_id": account_id or self.restore_account_id,
            "risk_state": risk_state or {},
            "realized_pnl": self.realized_pnl,
            "day_realized_pnl": self.day_realized_pnl,
            "session_date": self.session_date.isoformat() if self.session_date else None,
            "session_start_equity": self.session_start_equity,
            "peak_equity": self.peak_equity,
            "day_trade_dates": [d.isoformat() for d in self._day_trade_dates],
            "day_trades_today": self.day_trades_today,
            "trades": [t.model_dump(mode="json") for t in self.trades],
            "equity_curve": [[ts.isoformat(), value] for ts, value in self.equity_curve],
            "lots": {
                symbol: {
                    "qty": lot.qty,
                    "avg_price": lot.avg_price,
                    "entry_ts": lot.entry_ts.isoformat(),
                    "strategy": lot.strategy,
                    "telemetry": lot.telemetry.model_dump(),
                    "stop_loss": lot.stop_loss,
                    "initial_stop_loss": lot.initial_stop_loss,
                    "initial_risk_per_share": lot.initial_risk_per_share,
                    "initial_risk_dollars": lot.initial_risk_dollars,
                    "commission_basis": lot.commission_basis,
                    "closing_qty": lot.closing_qty,
                    "closing_notional": lot.closing_notional,
                    "realized_pnl_accum": lot.realized_pnl_accum,
                }
                for symbol, lot in self._lots.items()
            }
        }

    def restore_checkpoint(self, payload: dict) -> None:
        self.realized_pnl = float(payload.get("realized_pnl", 0.0))
        self.day_realized_pnl = float(payload.get("day_realized_pnl", 0.0))
        self.session_date = date.fromisoformat(payload["session_date"]) if payload.get("session_date") else None
        self.session_start_equity = float(payload["session_start_equity"]) if payload.get("session_start_equity") is not None else None
        restored_peak = float(payload.get("peak_equity", 0.0))
        self.peak_equity = max(restored_peak, self.session_start_equity) if self.session_start_equity is not None else restored_peak
        if "day_trade_dates" in payload:
            self._day_trade_dates = [date.fromisoformat(d) for d in payload.get("day_trade_dates", []) if d]
        elif payload.get("day_trades_today") and self.session_date:
            self._day_trade_dates = [self.session_date] * int(payload["day_trades_today"])
        else:
            self._day_trade_dates = []
        self.trades = [TradeRecord.model_validate(t) for t in payload.get("trades", [])]
        self.equity_curve = [(datetime.fromisoformat(ts), float(value)) for ts, value in payload.get("equity_curve", [])]
        self._lots = {}
        for symbol, raw in payload.get("lots", {}).items():
            avg_price = float(raw["avg_price"])
            qty = float(raw["qty"])
            current_stop = raw.get("stop_loss")
            initial_stop = raw.get("initial_stop_loss")
            initial_risk_per_share = raw.get("initial_risk_per_share")
            initial_risk_dollars = float(raw.get("initial_risk_dollars", 0.0) or 0.0)

            # Backward compatibility for checkpoints written before immutable
            # initial-stop/risk-dollar persistence existed.  The old checkpoint's
            # risk-per-share is safer evidence than a potentially ratcheted current
            # stop, so never infer original risk from current_stop when it exists.
            if initial_risk_per_share is not None:
                initial_risk_per_share = float(initial_risk_per_share)
            elif initial_stop is not None:
                initial_risk_per_share = abs(avg_price - float(initial_stop))
            if initial_risk_dollars <= 0 and initial_risk_per_share and abs(qty) > 0:
                initial_risk_dollars = abs(qty) * initial_risk_per_share
            if initial_stop is None and initial_risk_per_share and current_stop is not None:
                # Legacy checkpoints did not persist the original stop.  Preserve the
                # known R denominator but do not claim a ratcheted current stop was the
                # original entry stop unless its distance matches that denominator.
                candidate = float(current_stop)
                if abs(abs(avg_price - candidate) - initial_risk_per_share) <= 1e-8:
                    initial_stop = candidate

            self._lots[symbol.upper()] = _OpenLot(
                qty=qty,
                avg_price=avg_price,
                entry_ts=datetime.fromisoformat(raw["entry_ts"]) if raw.get("entry_ts") else datetime.now(UTC),
                strategy=raw.get("strategy", "reconciled"),
                stop_loss=current_stop,
                initial_stop_loss=initial_stop,
                initial_risk_per_share=initial_risk_per_share,
                initial_risk_dollars=initial_risk_dollars,
                commission_basis=float(raw.get("commission_basis", 0.0)),
                closing_qty=float(raw.get("closing_qty", 0.0)),
                closing_notional=float(raw.get("closing_notional", 0.0)),
                realized_pnl_accum=float(raw.get("realized_pnl_accum", 0.0)),
                telemetry=ExecutionTelemetry(**raw.get("telemetry", {}))
            )

    def on_fill(self, fill: Fill, peak_price: float | None = None, trough_price: float | None = None,
                initial_stop: float | None = None, risk_manager: Any | None = None, now: datetime | None = None,
                order: Any | None = None) -> TradeRecord | None:
        if self.restore_mode == "paper" and "simulation" not in getattr(fill, "broker_mode", ""):
            if getattr(fill, "broker_mode", "") not in ("paper", "shadow"):
                return None

        # For split/scaling orders, ensure we process the effective delta
        effective = Fill(
            order_id=fill.order_id, symbol=fill.symbol, side=fill.side,
            qty=fill.qty, price=fill.price, commission=fill.commission,
            strategy=fill.strategy, reason=fill.reason, ts=fill.ts,
            broker_fill_key=fill.broker_fill_key,
            broker_mode=fill.broker_mode,
            broker_account_id=fill.broker_account_id,
            role=fill.role,
        )
        if fill.cumulative_qty is not None and getattr(fill, "broker_fill_key", None):
            seen_key = f"{fill.order_id}_{fill.broker_fill_key}_{fill.cumulative_qty}"
            if seen_key in self._seen_fill_keys:
                return None
            
            import copy
            before = copy.deepcopy(self.__dict__)
            
            self._seen_fill_keys.add(seen_key)

            from app.db.database import Database
            db = Database.active() if getattr(fill, "broker_mode", None) else None

            if db is not None:
                account_id = fill.broker_account_id or "default_account"
                processed = db.processed_fill_qty(mode=fill.broker_mode, account_id=account_id, fill_key=fill.broker_fill_key)
                previous_notional = db.processed_fill_notional(mode=fill.broker_mode, account_id=account_id, fill_key=fill.broker_fill_key)
                delta = float(fill.cumulative_qty) - processed
                if delta <= 1e-9:
                    return None
                effective.qty = delta
                
                # In-memory tracking parity
                self._cumulative_fills[fill.order_id] = fill.cumulative_qty
                if getattr(fill, "cumulative_notional", None) is not None:
                    self._cumulative_notionals[fill.order_id] = fill.cumulative_notional

                try:
                    res = self._apply_fill(effective, peak_price, trough_price, initial_stop, order)
                    trade = res.closed_trade if isinstance(res, FillApplicationResult) else res
                    
                    risk_state = None
                    if trade is not None and risk_manager is not None:
                        risk_manager.on_trade_closed(trade, now=now)
                        risk_state = risk_manager.get_risk_state()
                    elif risk_manager is not None:
                        risk_state = risk_manager.get_risk_state()
                        
                    cumulative_notional = previous_notional + float(effective.qty) * float(effective.price)
                    db.commit_fill_processing(
                        fill_key=fill.broker_fill_key, 
                        cumulative_qty=float(fill.cumulative_qty), 
                        cumulative_notional=cumulative_notional, 
                        mode=fill.broker_mode, 
                        account_id=account_id, 
                        checkpoint=self.checkpoint(risk_state=risk_state), 
                        trade=trade
                    )
                    return trade
                except Exception:
                    self.__dict__.clear()
                    self.__dict__.update(before)
                    raise
            else:
                prev_cum = self._cumulative_fills.get(fill.order_id, 0.0)
                if fill.cumulative_qty > prev_cum:
                    effective.qty = fill.cumulative_qty - prev_cum
                    fill_cumulative_notional = getattr(fill, "cumulative_notional", None)
                    if fill_cumulative_notional is not None:
                        prev_notional = self._cumulative_notionals.get(fill.order_id, 0.0)
                        delta_notional = fill_cumulative_notional - prev_notional
                        effective.price = delta_notional / effective.qty if effective.qty > 0 else fill.price
                        self._cumulative_notionals[fill.order_id] = fill_cumulative_notional
                    self._cumulative_fills[fill.order_id] = fill.cumulative_qty
                    
                    if effective.qty > 1e-9:
                        try:
                            res = self._apply_fill(effective, peak_price, trough_price, initial_stop, order)
                            if isinstance(res, FillApplicationResult) and res.closed_trade:
                                if risk_manager:
                                    risk_manager.on_trade_closed(res.closed_trade, now=now)
                                return res.closed_trade
                        except Exception:
                            self.__dict__.clear()
                            self.__dict__.update(before)
                            raise
                return None

        res = self._apply_fill(fill, peak_price, trough_price, initial_stop, order)
        trade = res.closed_trade if isinstance(res, FillApplicationResult) else res
        if trade is not None and risk_manager is not None:
            risk_manager.on_trade_closed(trade, now=now)
        return trade

    def _apply_fill(self, fill: Fill, peak_price: float | None = None, trough_price: float | None = None,
                    initial_stop: float | None = None, order: Any | None = None) -> FillApplicationResult:
        symbol = fill.symbol.upper()
        signed = fill.qty if fill.side == OrderSide.BUY else -fill.qty
        lot = self._lots.get(symbol)
        
        if lot is None or abs(lot.qty) < 1e-9:
            # P0-0 Invariant: A missing local lot must never be sufficient evidence to create a new position.
            # Local position state is built solely by authoritative periodic reconciliation (engine_portfolio.py),
            # and entries should only be processed if they arrive as part of the normal lifecycle.
            # If the strategy was an exit/flatten, or the role is EXIT/TAKE_PROFIT/PROTECTION, do NOT create a phantom long/short.
            role_upper = getattr(fill, "role", "ENTRY").upper()
            if role_upper in ("EXIT", "TAKE_PROFIT", "PROTECTION"):
                applog.error("portfolio", f"[{fill.ts}] REJECTED phantom position creation: fill claims to exit/protect {symbol} (role={fill.role}) but no local lot exists.")
                return FillApplicationResult()
            if fill.reason and ("close" in fill.reason.lower() or "exit" in fill.reason.lower() or "flatten" in fill.reason.lower()):
                applog.error("portfolio", f"[{fill.ts}] REJECTED phantom position creation: fill claims to close/exit {symbol} but no local lot exists.")
                return FillApplicationResult()
            if fill.strategy and ("close" in fill.strategy.lower() or "exit" in fill.strategy.lower() or "flatten" in fill.strategy.lower()):
                applog.error("portfolio", f"[{fill.ts}] REJECTED phantom position creation: fill claims to close/exit {symbol} but no local lot exists.")
                return FillApplicationResult()

        if lot is None or abs(lot.qty) < 1e-9:
            self._lots[symbol] = _OpenLot(qty=signed, avg_price=fill.price, entry_ts=fill.ts, strategy=fill.strategy, commission_basis=fill.commission)
            if initial_stop is not None:
                self._lots[symbol].stop_loss = initial_stop
                self._lots[symbol].initial_stop_loss = initial_stop
                self._lots[symbol].initial_risk_per_share = abs(fill.price - initial_stop)
                self._lots[symbol].initial_risk_dollars = abs(signed) * abs(fill.price - initial_stop)
            if order is not None:
                self._lots[symbol].candidate_id = getattr(order, "candidate_id", None)
                self._lots[symbol].thesis_fingerprint = getattr(order, "thesis_fingerprint", None)
                self._lots[symbol].signal_generated_timestamp_utc = getattr(order, "signal_generated_timestamp_utc", None)
                self._lots[symbol].decision_completed_timestamp_utc = getattr(order, "decision_completed_timestamp_utc", None)
                self._lots[symbol].entry_quality_score = getattr(order, "entry_quality_score", None)
                self._lots[symbol].entry_quality = getattr(order, "entry_quality", None)
                self._lots[symbol].opportunity_state = getattr(order, "opportunity_state", None)
                self._lots[symbol].opportunity_score = getattr(order, "opportunity_score", None)
                self._lots[symbol].vwap_distance_pct = getattr(order, "vwap_distance_pct", None)
                self._lots[symbol].atr_extension = getattr(order, "atr_extension", None)
                self._lots[symbol].pre_entry_impulse_pct = getattr(order, "pre_entry_impulse_pct", None)
                self._lots[symbol].event_risk = getattr(order, "event_risk", "normal")
                self._lots[symbol].event_type = getattr(order, "event_type", None)
                self._lots[symbol].event_age_minutes = getattr(order, "event_age_minutes", None)
                self._lots[symbol].price_discovery = getattr(order, "price_discovery", False)
                self._lots[symbol].reentry_attempt = getattr(order, "reentry_attempt", 0)
                self._lots[symbol].same_thesis_blocked = getattr(order, "same_thesis_blocked", False)
                self._lots[symbol].trend_score = getattr(order, "trend_score", None)
                self._lots[symbol].location_score = getattr(order, "location_score", None)
                self._lots[symbol].extension_score = getattr(order, "extension_score", None)
                self._lots[symbol].structure_score = getattr(order, "structure_score", None)
            side_str = "BOUGHT" if signed > 0 else "SOLD SHORT"
            total_amt = abs(signed) * fill.price
            applog.info("portfolio", f"[{fill.ts}] {side_str} {symbol}: {abs(signed):g} shares @ {fill.price:.2f} (Total Amt: ${total_amt:,.2f} | {fill.strategy})")
            return FillApplicationResult()
        if (lot.qty > 0) == (signed > 0):
            total = lot.qty + signed
            lot.avg_price = (lot.avg_price * lot.qty + fill.price * signed) / total
            lot.qty = total
            lot.commission_basis += fill.commission
            # P0-1: Initial risk geometry is immutable. If it wasn't provided at entry, we do NOT 
            # allow trailing stops or scaling fills to retroactively infer it.
            side_str = "BOUGHT MORE" if signed > 0 else "SOLD MORE SHORT"
            total_amt = abs(signed) * fill.price
            applog.info("portfolio", f"[{fill.ts}] {side_str} {symbol}: {abs(signed):g} shares @ {fill.price:.2f} (Total Amt: ${total_amt:,.2f} | {fill.strategy})")
            return FillApplicationResult()

        original_direction = 1.0 if lot.qty > 0 else -1.0
        close_qty = min(abs(float(fill.qty)), abs(float(lot.qty)))
        entry_fee = lot.commission_basis * (close_qty / abs(lot.qty)) if lot.qty != 0 else 0.0
        close_fee = fill.commission * (close_qty / abs(signed)) if signed != 0 else 0.0
        pnl_fragment = ((float(fill.price) - lot.avg_price) * close_qty * original_direction - entry_fee - close_fee)
        lot.closing_qty += close_qty
        lot.closing_notional += float(fill.price) * close_qty
        lot.realized_pnl_accum += pnl_fragment
        self.realized_pnl += pnl_fragment
        self.day_realized_pnl += pnl_fragment
        remainder = lot.qty + signed
        if (remainder > 0) == (lot.qty > 0) and abs(remainder) >= 1e-9:
            lot.qty = remainder
            lot.commission_basis -= entry_fee
            return FillApplicationResult(realized_pnl_fragment=pnl_fragment)
        avg_exit = lot.closing_notional / lot.closing_qty if lot.closing_qty > 0 else float(fill.price)
        basis = lot.avg_price * lot.closing_qty
        pnl = lot.realized_pnl_accum
        init_risk_dollars = lot.initial_risk_dollars
        if init_risk_dollars > 0:
            r_multiple = round(pnl / init_risk_dollars, 4)
            init_risk = init_risk_dollars / lot.closing_qty if lot.closing_qty > 0 else lot.initial_risk_per_share
        else:
            r_multiple = None
            init_risk = None
        best = peak_price if peak_price is not None else fill.price
        worst = trough_price if trough_price is not None else fill.price
        mfe_val = (best - lot.avg_price) if original_direction > 0 else (lot.avg_price - best)
        mae_val = (lot.avg_price - worst) if original_direction > 0 else (worst - lot.avg_price)
        mfe_pct = (mfe_val / lot.avg_price * 100.0) if lot.avg_price > 0 else 0.0
        mae_pct = (mae_val / lot.avg_price * 100.0) if lot.avg_price > 0 else 0.0
        mfe_r = (mfe_val / init_risk) if init_risk and init_risk > 0 else 0.0
        winner_cap = (r_multiple / mfe_r) if r_multiple is not None and mfe_r > 0 else 0.0
        trade = TradeRecord(symbol=symbol, side="long" if original_direction > 0 else "short", qty=lot.closing_qty, entry_price=round(lot.avg_price, 4), exit_price=round(avg_exit, 4), entry_at=lot.entry_ts, exit_at=fill.ts, pnl=round(pnl, 2), pnl_pct=round(pnl / basis * 100.0, 3) if basis else 0.0, r_multiple=r_multiple, candidate_id=lot.candidate_id or getattr(fill, "client_order_id", getattr(fill, "order_id", None)), thesis_fingerprint=lot.thesis_fingerprint, initial_stop=round(lot.initial_stop_loss, 4) if lot.initial_stop_loss is not None else None, initial_risk=round(init_risk, 4) if init_risk is not None else None, strategy=lot.strategy, exit_reason=fill.reason or "exit", mfe=round(mfe_val, 4), mae=round(mae_val, 4), mfe_pct=round(mfe_pct, 4), mae_pct=round(mae_pct, 4), capture_efficiency=round(winner_cap, 4), telemetry=lot.telemetry, signal_generated_timestamp_utc=lot.signal_generated_timestamp_utc, decision_completed_timestamp_utc=lot.decision_completed_timestamp_utc, entry_quality_score=lot.entry_quality_score, entry_quality=lot.entry_quality, opportunity_state=lot.opportunity_state, opportunity_score=lot.opportunity_score, vwap_distance_pct=lot.vwap_distance_pct, atr_extension=lot.atr_extension, pre_entry_impulse_pct=lot.pre_entry_impulse_pct, event_risk=lot.event_risk, event_type=lot.event_type, event_age_minutes=lot.event_age_minutes, price_discovery=lot.price_discovery, reentry_attempt=lot.reentry_attempt, same_thesis_blocked=lot.same_thesis_blocked, trend_score=lot.trend_score, location_score=lot.location_score, extension_score=lot.extension_score, structure_score=lot.structure_score)
        del self._lots[symbol]
        self.trades.append(trade)
        if trade.entry_at.date() == trade.exit_at.date():
            self._day_trade_dates.append(trade.exit_at.date())
        if abs(remainder) >= 1e-9:
            remaining_fill_fee = fill.commission - close_fee
            self._lots[symbol] = _OpenLot(qty=remainder, avg_price=fill.price, entry_ts=fill.ts, strategy=fill.strategy, commission_basis=remaining_fill_fee)
        action_str = "SOLD" if trade.side == "long" else "BOUGHT TO COVER"
        pct_str = f"{trade.pnl_pct:+.2f}%" if trade.pnl_pct else "0.0%"
        
        r_str = f" | {trade.r_multiple:+.2f}R" if trade.r_multiple is not None else ""
        
        if trade.initial_stop and trade.initial_risk:
            risk_src = "INITIAL_LOCKED"
            stop_str = f" [Entry: {lot.avg_price:.2f} | Stop: {trade.initial_stop:.2f} | Risk/Sh: {trade.initial_risk:.2f} | Risk Source: {risk_src}]"
        else:
            risk_src = "UNKNOWN"
            stop_str = f" [Entry: {lot.avg_price:.2f} | Stop: UNKNOWN | Risk/Sh: UNKNOWN | Risk Source: {risk_src}]"
            
        applog.info("portfolio", f"[{fill.ts}] {action_str} {symbol}: {lot.closing_qty:g} shares @ {avg_exit:.2f} -> P&L: ${pnl:+.2f} ({pct_str}){r_str}{stop_str} ({fill.reason or 'exit'}) | Entered: {trade.entry_at}")
        return FillApplicationResult(realized_pnl_fragment=pnl_fragment, closed_trade=trade, position_closed=True)

    def stats(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {"total_trades": len(self.trades), "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins) / len(self.trades) * 100.0, 1) if self.trades else 0.0, "gross_profit": round(gross_win, 2), "gross_loss": round(gross_loss, 2), "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None, "realized_pnl": round(self.realized_pnl, 2), "avg_win": round(gross_win / len(wins), 2) if wins else 0.0, "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0, "day_trades_today": self.day_trades_today}
