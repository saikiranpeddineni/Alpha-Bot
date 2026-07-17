"""Portfolio tracker.

Consumes broker fills, maintains an entry ledger, emits closed TradeRecords,
and tracks the equity curve plus session statistics (day P&L, peak equity,
day-trade count). Broker-agnostic: works identically for the paper simulator,
Alpaca, and the backtester.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
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
    initial_risk_per_share: float | None = None
    commission_basis: float = 0.0


@dataclass
class PortfolioTracker:
    max_curve_points: int = 5000
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

    # ------------------------------------------------------------------
    # PDT rolling-window counter
    # ------------------------------------------------------------------

    def day_trades_in_window(self, as_of: date | None = None) -> int:
        """Count intraday round-trips inside the rolling 5-business-day window.

        *as_of* defaults to ``date.today()`` when omitted, but callers that
        know the authoritative session date (e.g. the trading engine) should
        pass it explicitly so the count is deterministic in tests and replay.

        The window is *inclusive* of *as_of* and extends back to the date
        returned by :func:`_business_days_ago(as_of, 4)` (i.e., today plus
        4 prior business days = 5 business days total), matching the SEC PDT
        rolling-window definition.
        """
        ref = as_of if as_of is not None else date.today()
        window_start = _business_days_ago(ref, _PDT_WINDOW_BUSINESS_DAYS - 1)
        return sum(1 for d in self._day_trade_dates if d >= window_start)

    @property
    def day_trades_today(self) -> int:  # noqa: D102  (backwards-compat property)
        """Backwards-compatible property: count for the current session.

        New code should prefer :meth:`day_trades_in_window` with an explicit
        reference date for PDT checks.  This property exists so legacy callers
        continue to get the intraday session trade count.
        """
        ref = self.session_date
        if ref is None:
            if self._day_trade_dates:
                ref = self._day_trade_dates[-1]
            else:
                from datetime import date
                ref = date.today()
        return sum(1 for d in self._day_trade_dates if d == ref)

    @day_trades_today.setter
    def day_trades_today(self, val: int) -> None:
        from datetime import date
        today = self.session_date or date.today()
        self._day_trade_dates = [d for d in self._day_trade_dates if d != today]
        for _ in range(val):
            self._day_trade_dates.append(today)

    def roll_session(self, session: date, equity: float) -> RollResult:
        """Advance to a new trading session.

        If open lots exist at the session boundary they are logged at WARNING
        level so operators can see the overnight carry-forward exposure.
        Intraday P&L counter (``day_realized_pnl``) is reset; the PDT
        rolling log is *not* cleared — only entries older than the 5-
        business-day window are pruned to bound memory growth.

        Returns a :class:`RollResult` that is truthy when the session
        actually advanced (backwards-compatible with the previous ``bool``
        return contract).
        """
        if self.session_date == session:
            return RollResult(rolled=False)

        # Identify open lots being carried into the new session.
        carry: list[str] = []
        for symbol, lot in self._lots.items():
            if abs(lot.qty) >= 1e-9:
                carry.append(symbol)
                direction = "LONG" if lot.qty > 0 else "SHORT"
                applog.warning(
                    "portfolio",
                    f"Session roll {self.session_date} -> {session}: carrying "
                    f"{direction} {abs(lot.qty):g} {symbol} @ avg "
                    f"{lot.avg_price:.4f} into new session. "
                    "New session_start_equity reflects overnight P&L in equity baseline.",
                )

        self.session_date = session
        self.session_start_equity = equity
        self.day_realized_pnl = 0.0
        # Prune day-trade log: keep only entries within the rolling window.
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

    def note_stop(self, symbol: str, stop_loss: float | None) -> None:
        lot = self._lots.get(symbol.upper())
        if lot and stop_loss is not None:
            lot.stop_loss = stop_loss
            lot.initial_risk_per_share = abs(lot.avg_price - stop_loss)

    def open_positions(self) -> dict[str, _OpenLot]:
        """Return a snapshot of all symbols with non-zero open lots."""
        return {s: lot for s, lot in self._lots.items() if abs(lot.qty) >= 1e-9}

    def on_fill(self, fill: Fill, peak_price: float | None = None, trough_price: float | None = None, initial_stop: float | None = None) -> TradeRecord | None:
        symbol = fill.symbol.upper()
        signed = fill.qty if fill.side == OrderSide.BUY else -fill.qty
        lot = self._lots.get(symbol)
        if lot is None or abs(lot.qty) < 1e-9:
            self._lots[symbol] = _OpenLot(qty=signed, avg_price=fill.price, entry_ts=fill.ts, strategy=fill.strategy, commission_basis=fill.commission)
            if initial_stop is not None:
                self._lots[symbol].stop_loss = initial_stop
                self._lots[symbol].initial_risk_per_share = abs(fill.price - initial_stop)
            side_str = "BOUGHT" if signed > 0 else "SOLD SHORT"
            total_amt = abs(signed) * fill.price
            applog.info("portfolio", f"[{fill.ts}] {side_str} {symbol}: {abs(signed):g} shares @ {fill.price:.2f} (Total Amt: ${total_amt:,.2f} | {fill.strategy})")
            return None

        if (lot.qty > 0) == (signed > 0):
            total = lot.qty + signed
            lot.avg_price = (lot.avg_price * lot.qty + fill.price * signed) / total
            lot.qty = total
            lot.commission_basis += fill.commission
            side_str = "BOUGHT MORE" if signed > 0 else "SOLD MORE SHORT"
            total_amt = abs(signed) * fill.price
            applog.info("portfolio", f"[{fill.ts}] {side_str} {symbol}: {abs(signed):g} shares @ {fill.price:.2f} (Total Amt: ${total_amt:,.2f} | {fill.strategy})")
            return None

        closed_qty = min(abs(lot.qty), abs(signed))
        direction = 1.0 if lot.qty > 0 else -1.0
        entry_fee = lot.commission_basis * (closed_qty / abs(lot.qty))
        close_fee = fill.commission * (closed_qty / abs(signed))
        pnl = (fill.price - lot.avg_price) * closed_qty * direction - entry_fee - close_fee
        basis = lot.avg_price * closed_qty
        r_multiple = None
        init_risk = lot.initial_risk_per_share
        if initial_stop is not None:
            init_risk = abs(lot.avg_price - initial_stop)
        if init_risk:
            r_multiple = round(pnl / (init_risk * closed_qty), 2)
        if direction > 0:
            best = peak_price if peak_price is not None else max(lot.avg_price, fill.price)
            worst = trough_price if trough_price is not None else min(lot.avg_price, fill.price)
            mfe_val = best - lot.avg_price
            mae_val = lot.avg_price - worst
        else:
            best = trough_price if trough_price is not None else min(lot.avg_price, fill.price)
            worst = peak_price if peak_price is not None else max(lot.avg_price, fill.price)
            mfe_val = lot.avg_price - best
            mae_val = worst - lot.avg_price
        mfe_pct = (mfe_val / lot.avg_price * 100.0) if lot.avg_price > 0 else 0.0
        mae_pct = (mae_val / lot.avg_price * 100.0) if lot.avg_price > 0 else 0.0
        mfe_r = (mfe_val / init_risk) if init_risk and init_risk > 0 else 0.0
        mae_r = (mae_val / init_risk) if init_risk and init_risk > 0 else 0.0
        winner_cap = (r_multiple / mfe_r) if r_multiple is not None and mfe_r > 0 else 0.0
        trade = TradeRecord(symbol=symbol, side="long" if direction > 0 else "short", qty=closed_qty, entry_price=round(lot.avg_price, 4), exit_price=round(fill.price, 4), entry_at=lot.entry_ts, exit_at=fill.ts, pnl=round(pnl, 2), pnl_pct=round(pnl / basis * 100.0, 3) if basis else 0.0, r_multiple=r_multiple, strategy=lot.strategy, exit_reason=fill.reason or "exit", mfe=round(mfe_val, 4), mae=round(mae_val, 4), mfe_pct=round(mfe_pct, 4), mae_pct=round(mae_pct, 4), capture_efficiency=round(winner_cap, 4))
        self.trades.append(trade)
        self.realized_pnl += pnl
        self.day_realized_pnl += pnl
        # PDT: record the date of every intraday round-trip.
        if trade.entry_at.date() == trade.exit_at.date():
            self._day_trade_dates.append(trade.exit_at.date())

        remainder = lot.qty + signed
        remaining_entry_fee = lot.commission_basis - entry_fee
        remaining_fill_fee = fill.commission - close_fee
        if abs(remainder) < 1e-9:
            del self._lots[symbol]
        elif (remainder > 0) == (lot.qty > 0):
            lot.qty = remainder
            lot.commission_basis = remaining_entry_fee
        else:
            self._lots[symbol] = _OpenLot(qty=remainder, avg_price=fill.price, entry_ts=fill.ts, strategy=fill.strategy, commission_basis=remaining_fill_fee)
        action_str = "SOLD" if trade.side == "long" else "BOUGHT TO COVER"
        pct_str = f"{trade.pnl_pct:+.2f}%" if trade.pnl_pct else "0.0%"
        if pnl > 0:
            applog.info("portfolio", f"[{fill.ts}] {action_str} {symbol}: {closed_qty:g} shares @ {fill.price:.2f} -> P&L: +${pnl:,.2f} ({pct_str}) ({fill.reason or 'exit'}) | Entered: {trade.entry_at}")
        else:
            applog.info("portfolio", f"[{fill.ts}] {action_str} {symbol}: {closed_qty:g} shares @ {fill.price:.2f} -> P&L: -${abs(pnl):,.2f} ({pct_str}) ({fill.reason or 'exit'}) | Entered: {trade.entry_at}")
        return trade

    def stats(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {"total_trades": len(self.trades), "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins) / len(self.trades) * 100.0, 1) if self.trades else 0.0, "gross_profit": round(gross_win, 2), "gross_loss": round(gross_loss, 2), "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None, "realized_pnl": round(self.realized_pnl, 2), "avg_win": round(gross_win / len(wins), 2) if wins else 0.0, "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0, "day_trades_today": self.day_trades_today}
