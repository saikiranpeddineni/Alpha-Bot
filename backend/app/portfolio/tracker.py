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
from typing import NamedTuple

from app.brokers.base import Fill
from app.core.domain import OrderSide, TradeRecord
from app.core.logging import applog


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
    closing_notional: float = 0.0
    realized_pnl_accum: float = 0.0


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


    # ------------------------------------------------------------------
    # PDT rolling-window counter
    # ------------------------------------------------------------------

    def day_trades_in_window(self, as_of: date | datetime | None = None) -> int:
        """Count intraday round-trips inside the rolling 5-business-day window.

        *as_of* defaults to ``date.today()`` when omitted, but callers that
        know the authoritative session date (e.g. the trading engine) should
        pass it explicitly so the count is deterministic in tests and replay.

        The window is *inclusive* of *as_of* and extends back to the date
        returned by :func:`_business_days_ago(as_of, 4)` (i.e., today plus
        4 prior business days = 5 business days total), matching the SEC PDT
        rolling-window definition.
        """
        session = as_of or self.session_date or (
            self._day_trade_dates[-1]
            if self._day_trade_dates
            else date.today()
        )

        if isinstance(session, datetime):
            session = session.date()

        window_start = _business_days_ago(
            session,
            _PDT_WINDOW_BUSINESS_DAYS - 1,
        )

        return sum(
            1
            for d in self._day_trade_dates
            if window_start <= d <= session
        )

    @property
    def day_trades_today(self) -> int:
        """Backwards-compatible convenience accessor using session date or fallback."""
        ref = self.session_date or (self._day_trade_dates[-1] if self._day_trade_dates else date.today())
        return sum(1 for d in self._day_trade_dates if d == ref)

    @day_trades_today.setter
    def day_trades_today(self, value: int) -> None:
        """Backwards-compatible setter for tests and checkpoint restore."""
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
        lot = self._lots.get(symbol.upper())
        if lot and stop_loss is not None:
            lot.stop_loss = stop_loss
            lot.initial_risk_per_share = abs(lot.avg_price - stop_loss)

    def open_positions(self) -> dict[str, _OpenLot]:
        """Return a snapshot of all symbols with non-zero open lots."""
        return {s: lot for s, lot in self._lots.items() if abs(lot.qty) >= 1e-9}

    def checkpoint(self, account_id: str | None = None, risk_state: dict | None = None) -> dict:
        return {
            # fix(#61): embed account_id in checkpoint for cross-account validation on restore
            "account_id": account_id or self.restore_account_id,
            # fix(#43): embed safety-critical risk state (loss streak, cooldowns, halts)
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
                    "stop_loss": lot.stop_loss,
                    "initial_risk_per_share": lot.initial_risk_per_share,
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
        if self.session_start_equity is not None:
            self.peak_equity = max(restored_peak, self.session_start_equity)
        else:
            self.peak_equity = restored_peak
        if "day_trade_dates" in payload:
            self._day_trade_dates = [date.fromisoformat(d) for d in payload.get("day_trade_dates", []) if d]
        elif payload.get("day_trades_today") and self.session_date:
            cnt = int(payload["day_trades_today"])
            self._day_trade_dates = [self.session_date] * cnt
        else:
            self._day_trade_dates = []
        self.trades = [TradeRecord.model_validate(t) for t in payload.get("trades", [])]
        self.equity_curve = [(datetime.fromisoformat(ts), float(value)) for ts, value in payload.get("equity_curve", [])]
        self._lots = {
            symbol.upper(): _OpenLot(
                qty=float(lot["qty"]),
                avg_price=float(lot["avg_price"]),
                entry_ts=datetime.fromisoformat(lot["entry_ts"]) if lot.get("entry_ts") else datetime.now(UTC),
                strategy=lot.get("strategy", "reconciled"),
                stop_loss=lot.get("stop_loss"),
                initial_risk_per_share=lot.get("initial_risk_per_share"),
                commission_basis=float(lot.get("commission_basis", 0.0)),
                closing_qty=float(lot.get("closing_qty", 0.0)),
                closing_notional=float(lot.get("closing_notional", 0.0)),
                realized_pnl_accum=float(lot.get("realized_pnl_accum", 0.0)),
            )
            for symbol, lot in payload.get("lots", {}).items()
        }

    def on_fill(self, fill: Fill, peak_price: float | None = None, trough_price: float | None = None,
                initial_stop: float | None = None, risk_state: dict | None = None,
                risk_manager: Any | None = None, now: datetime | None = None) -> TradeRecord | None:
        if fill.broker_fill_key and fill.cumulative_qty is not None and fill.broker_mode:
            from app.db.database import Database
            db = Database.active()
            if db is not None:
                processed = db.processed_fill_qty(
                    mode=fill.broker_mode,
                    account_id=fill.broker_account_id or "default_account",
                    fill_key=fill.broker_fill_key
                )
                delta = float(fill.cumulative_qty) - processed
                if delta <= 1e-9:
                    return None
                effective = copy.copy(fill)
                effective.qty = delta
                before = copy.deepcopy(self.__dict__)
                try:
                    res = self._apply_fill(effective, peak_price, trough_price, initial_stop)
                    trade = res.closed_trade if isinstance(res, FillApplicationResult) else res

                    # fix(#43): invoke risk manager on trade close BEFORE capturing checkpoint
                    # so post-close loss-streak & cooldown state are saved atomically in DB
                    if trade is not None and risk_manager is not None:
                        risk_manager.on_trade_closed(trade, now=now)
                        risk_state = risk_manager.get_risk_state()
                    elif risk_state is None and risk_manager is not None:
                        risk_state = risk_manager.get_risk_state()

                    db.commit_fill_processing(
                        fill_key=fill.broker_fill_key,
                        cumulative_qty=float(fill.cumulative_qty),
                        mode=fill.broker_mode,
                        account_id=fill.broker_account_id or "default_account",
                        checkpoint=self.checkpoint(risk_state=risk_state),
                        trade=trade
                    )
                    return trade
                except Exception:
                    self.__dict__.clear()
                    self.__dict__.update(before)
                    raise


        res = self._apply_fill(fill, peak_price, trough_price, initial_stop)
        trade = res.closed_trade if isinstance(res, FillApplicationResult) else res

        if trade is not None and risk_manager is not None:
            risk_manager.on_trade_closed(trade, now=now)

        return trade

    def _apply_fill(
        self,
        fill: Fill,
        peak_price: float | None = None,
        trough_price: float | None = None,
        initial_stop: float | None = None,
    ) -> FillApplicationResult:
        symbol = fill.symbol.upper()
        signed = fill.qty if fill.side == OrderSide.BUY else -fill.qty
        lot = self._lots.get(symbol)
        if lot is None or abs(lot.qty) < 1e-9:
            self._lots[symbol] = _OpenLot(
                qty=signed,
                avg_price=fill.price,
                entry_ts=fill.ts,
                strategy=fill.strategy,
                commission_basis=fill.commission,
            )
            if initial_stop is not None:
                self._lots[symbol].stop_loss = initial_stop
                self._lots[symbol].initial_stop_loss = initial_stop
                self._lots[symbol].initial_risk_per_share = abs(fill.price - initial_stop)
                self._lots[symbol].initial_risk_dollars = abs(signed) * abs(fill.price - initial_stop)
            side_str = "BOUGHT" if signed > 0 else "SOLD SHORT"
            total_amt = abs(signed) * fill.price
            applog.info("portfolio", f"[{fill.ts}] {side_str} {symbol}: {abs(signed):g} shares @ {fill.price:.2f} (Total Amt: ${total_amt:,.2f} | {fill.strategy})")
            return FillApplicationResult(realized_pnl_fragment=0.0, closed_trade=None, position_closed=False)

        if (lot.qty > 0) == (signed > 0):
            total = lot.qty + signed
            lot.avg_price = (lot.avg_price * lot.qty + fill.price * signed) / total
            lot.qty = total
            lot.commission_basis += fill.commission
            if initial_stop is not None:
                lot.stop_loss = initial_stop
                lot.initial_stop_loss = initial_stop
                added_risk_dollars = abs(signed) * abs(fill.price - initial_stop)
                lot.initial_risk_dollars += added_risk_dollars
                lot.initial_risk_per_share = lot.initial_risk_dollars / abs(total) if total != 0 else abs(lot.avg_price - initial_stop)
            side_str = "BOUGHT MORE" if signed > 0 else "SOLD MORE SHORT"
            total_amt = abs(signed) * fill.price
            applog.info("portfolio", f"[{fill.ts}] {side_str} {symbol}: {abs(signed):g} shares @ {fill.price:.2f} (Total Amt: ${total_amt:,.2f} | {fill.strategy})")
            return FillApplicationResult(realized_pnl_fragment=0.0, closed_trade=None, position_closed=False)

        original_direction = 1.0 if lot.qty > 0 else -1.0
        close_qty = min(abs(float(fill.qty)), abs(float(lot.qty)))

        entry_fee = lot.commission_basis * (close_qty / abs(lot.qty)) if lot.qty != 0 else 0.0
        close_fee = fill.commission * (close_qty / abs(signed)) if signed != 0 else 0.0

        pnl_fragment = (
            (float(fill.price) - lot.avg_price) * close_qty * original_direction
            - entry_fee
            - close_fee
        )

        lot.closing_qty += close_qty
        lot.closing_notional += float(fill.price) * close_qty
        lot.realized_pnl_accum += pnl_fragment
        self.realized_pnl += pnl_fragment
        self.day_realized_pnl += pnl_fragment

        remainder = lot.qty + signed

        if (remainder > 0) == (lot.qty > 0) and abs(remainder) >= 1e-9:
            lot.qty = remainder
            lot.commission_basis -= entry_fee
            return FillApplicationResult(
                realized_pnl_fragment=pnl_fragment,
                closed_trade=None,
                position_closed=False,
            )

        avg_exit = (
            lot.closing_notional / lot.closing_qty
            if lot.closing_qty > 0
            else float(fill.price)
        )
        basis = lot.avg_price * lot.closing_qty
        pnl = lot.realized_pnl_accum

        if lot.initial_stop_loss is None and initial_stop is not None:
            lot.initial_stop_loss = initial_stop
            if not lot.initial_risk_per_share and lot.avg_price:
                lot.initial_risk_per_share = abs(lot.avg_price - initial_stop)
            if not lot.initial_risk_dollars and lot.avg_price:
                lot.initial_risk_dollars = abs(lot.closing_qty or lot.qty) * abs(lot.avg_price - initial_stop)

        init_risk_dollars = lot.initial_risk_dollars
        if init_risk_dollars > 0:
            r_multiple = round(pnl / init_risk_dollars, 4)
            init_risk = init_risk_dollars / lot.closing_qty if lot.closing_qty > 0 else lot.initial_risk_per_share
        else:
            init_risk = lot.initial_risk_per_share
            r_multiple = round(pnl / (init_risk * lot.closing_qty), 4) if (init_risk and lot.closing_qty > 0) else None

        best = peak_price if peak_price is not None else fill.price
        worst = trough_price if trough_price is not None else fill.price
        mfe_val = (best - lot.avg_price) if original_direction > 0 else (lot.avg_price - best)
        mae_val = (lot.avg_price - worst) if original_direction > 0 else (worst - lot.avg_price)
        mfe_pct = (mfe_val / lot.avg_price * 100.0) if lot.avg_price > 0 else 0.0
        mae_pct = (mae_val / lot.avg_price * 100.0) if lot.avg_price > 0 else 0.0
        mfe_r = (mfe_val / init_risk) if init_risk and init_risk > 0 else 0.0
        winner_cap = (r_multiple / mfe_r) if r_multiple is not None and mfe_r > 0 else 0.0

        trade = TradeRecord(
            symbol=symbol,
            side="long" if original_direction > 0 else "short",
            qty=lot.closing_qty,
            entry_price=round(lot.avg_price, 4),
            exit_price=round(avg_exit, 4),
            entry_at=lot.entry_ts,
            exit_at=fill.ts,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl / basis * 100.0, 3) if basis else 0.0,
            r_multiple=r_multiple,
            candidate_id=getattr(fill, "client_order_id", getattr(fill, "order_id", None)),
            initial_stop=round(lot.initial_stop_loss, 4) if lot.initial_stop_loss is not None else None,
            initial_risk=round(init_risk, 4) if init_risk is not None else None,
            strategy=lot.strategy,
            exit_reason=fill.reason or "exit",
            mfe=round(mfe_val, 4),
            mae=round(mae_val, 4),
            mfe_pct=round(mfe_pct, 4),
            mae_pct=round(mae_pct, 4),
            capture_efficiency=round(winner_cap, 4),
        )

        del self._lots[symbol]
        self.trades.append(trade)

        if trade.entry_at.date() == trade.exit_at.date():
            self._day_trade_dates.append(trade.exit_at.date())

        if abs(remainder) >= 1e-9:
            # Position flip: previous lot closed, new lot created for remaining opposite qty
            remaining_fill_fee = fill.commission - close_fee
            self._lots[symbol] = _OpenLot(
                qty=remainder,
                avg_price=fill.price,
                entry_ts=fill.ts,
                strategy=fill.strategy,
                commission_basis=remaining_fill_fee,
            )

        action_str = "SOLD" if trade.side == "long" else "BOUGHT TO COVER"
        pct_str = f"{trade.pnl_pct:+.2f}%" if trade.pnl_pct else "0.0%"
        applog.info("portfolio", f"[{fill.ts}] {action_str} {symbol}: {lot.closing_qty:g} shares @ {avg_exit:.2f} -> P&L: {pnl:+.2f} ({pct_str}) ({fill.reason or 'exit'}) | Entered: {trade.entry_at}")
        return FillApplicationResult(
            realized_pnl_fragment=pnl_fragment,
            closed_trade=trade,
            position_closed=True,
        )

    def stats(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {"total_trades": len(self.trades), "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins) / len(self.trades) * 100.0, 1) if self.trades else 0.0, "gross_profit": round(gross_win, 2), "gross_loss": round(gross_loss, 2), "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None, "realized_pnl": round(self.realized_pnl, 2), "avg_win": round(gross_win / len(wins), 2) if wins else 0.0, "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0, "day_trades_today": self.day_trades_today}
