"""Portfolio tracker.

Consumes broker fills, maintains an entry ledger, emits closed TradeRecords,
and tracks the equity curve plus session statistics. Broker-backed execution
watermarks are committed atomically with a durable portfolio checkpoint so an
Alpaca fill can be replayed exactly once after process restart.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime

from app.brokers.base import Fill
from app.core.domain import OrderSide, TradeRecord
from app.core.logging import applog


@dataclass
class _OpenLot:
    qty: float
    avg_price: float
    entry_ts: datetime
    strategy: str
    stop_loss: float | None = None
    initial_risk_per_share: float | None = None


@dataclass
class PortfolioTracker:
    max_curve_points: int = 5000
    restore_mode: str | None = None
    realized_pnl: float = 0.0
    day_realized_pnl: float = 0.0
    session_date: date | None = None
    session_start_equity: float | None = None
    peak_equity: float = 0.0
    day_trades_today: int = 0
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
            self.restore_checkpoint(checkpoint)
            applog.info("portfolio", f"Restored durable {self.restore_mode} portfolio checkpoint")

    def checkpoint(self) -> dict:
        return {
            "realized_pnl": self.realized_pnl,
            "day_realized_pnl": self.day_realized_pnl,
            "session_date": self.session_date.isoformat() if self.session_date else None,
            "session_start_equity": self.session_start_equity,
            "peak_equity": self.peak_equity,
            "day_trades_today": self.day_trades_today,
            "trades": [t.model_dump(mode="json") for t in self.trades],
            "equity_curve": [[ts.isoformat(), value] for ts, value in self.equity_curve],
            "lots": {
                symbol: {
                    "qty": lot.qty, "avg_price": lot.avg_price, "entry_ts": lot.entry_ts.isoformat(),
                    "strategy": lot.strategy, "stop_loss": lot.stop_loss,
                    "initial_risk_per_share": lot.initial_risk_per_share,
                }
                for symbol, lot in self._lots.items()
            },
        }

    def restore_checkpoint(self, payload: dict) -> None:
        self.realized_pnl = float(payload.get("realized_pnl", 0.0))
        self.day_realized_pnl = float(payload.get("day_realized_pnl", 0.0))
        self.session_date = date.fromisoformat(payload["session_date"]) if payload.get("session_date") else None
        self.session_start_equity = payload.get("session_start_equity")
        self.peak_equity = float(payload.get("peak_equity", 0.0))
        self.day_trades_today = int(payload.get("day_trades_today", 0))
        self.trades = [TradeRecord.model_validate(t) for t in payload.get("trades", [])]
        self.equity_curve = [(datetime.fromisoformat(ts), float(value)) for ts, value in payload.get("equity_curve", [])]
        self._lots = {
            symbol.upper(): _OpenLot(
                qty=float(lot["qty"]), avg_price=float(lot["avg_price"]),
                entry_ts=datetime.fromisoformat(lot["entry_ts"]), strategy=lot["strategy"],
                stop_loss=lot.get("stop_loss"), initial_risk_per_share=lot.get("initial_risk_per_share"),
            )
            for symbol, lot in payload.get("lots", {}).items()
        }

    def roll_session(self, session: date, equity: float) -> bool:
        if self.session_date == session:
            return False
        self.session_date = session
        self.session_start_equity = equity
        self.day_realized_pnl = 0.0
        self.day_trades_today = 0
        return True

    def mark_equity(self, equity: float, ts: datetime) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        curve = self.equity_curve
        if curve and curve[-1][0] == ts:
            curve[-1] = (ts, equity)
        else:
            curve.append((ts, equity))
        if len(curve) > self.max_curve_points:
            self.equity_curve = curve[: len(curve) // 2 : 2] + curve[len(curve) // 2 :]

    def day_pnl(self, equity: float) -> float:
        return 0.0 if self.session_start_equity is None else equity - self.session_start_equity

    def drawdown_pct(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity * 100.0)

    def note_stop(self, symbol: str, stop_loss: float | None) -> None:
        lot = self._lots.get(symbol.upper())
        if lot and stop_loss is not None:
            lot.stop_loss = stop_loss
            lot.initial_risk_per_share = abs(lot.avg_price - stop_loss)

    def on_fill(self, fill: Fill, peak_price: float | None = None, trough_price: float | None = None,
                initial_stop: float | None = None) -> TradeRecord | None:
        if fill.broker_fill_key and fill.cumulative_qty is not None and fill.broker_mode:
            from app.db.database import Database
            db = Database.active()
            if db is not None:
                processed = db.processed_fill_qty(fill.broker_fill_key)
                delta = float(fill.cumulative_qty) - processed
                if delta <= 1e-9:
                    return None
                effective = fill.model_copy(update={"qty": delta})
                before = deepcopy(self.__dict__)
                try:
                    trade = self._apply_fill(effective, peak_price, trough_price, initial_stop)
                    db.commit_fill_processing(fill_key=fill.broker_fill_key, cumulative_qty=float(fill.cumulative_qty),
                                              mode=fill.broker_mode, checkpoint=self.checkpoint(), trade=trade)
                    return trade
                except Exception:
                    self.__dict__.clear()
                    self.__dict__.update(before)
                    raise
        return self._apply_fill(fill, peak_price, trough_price, initial_stop)

    def _apply_fill(self, fill: Fill, peak_price: float | None = None, trough_price: float | None = None,
                    initial_stop: float | None = None) -> TradeRecord | None:
        symbol = fill.symbol.upper()
        signed = fill.qty if fill.side == OrderSide.BUY else -fill.qty
        lot = self._lots.get(symbol)
        if lot is None or abs(lot.qty) < 1e-9:
            self._lots[symbol] = _OpenLot(qty=signed, avg_price=fill.price, entry_ts=fill.ts, strategy=fill.strategy)
            if initial_stop is not None:
                self._lots[symbol].stop_loss = initial_stop
                self._lots[symbol].initial_risk_per_share = abs(fill.price - initial_stop)
            side_str = "BOUGHT" if signed > 0 else "SOLD SHORT"
            applog.info("portfolio", f"[{fill.ts}] {side_str} {symbol}: {abs(signed):g} shares @ {fill.price:.2f} "
                                    f"(Total Amt: ${abs(signed) * fill.price:,.2f} | {fill.strategy})")
            return None
        if (lot.qty > 0) == (signed > 0):
            total = lot.qty + signed
            lot.avg_price = (lot.avg_price * lot.qty + fill.price * signed) / total
            lot.qty = total
            side_str = "BOUGHT MORE" if signed > 0 else "SOLD MORE SHORT"
            applog.info("portfolio", f"[{fill.ts}] {side_str} {symbol}: {abs(signed):g} shares @ {fill.price:.2f} "
                                    f"(Total Amt: ${abs(signed) * fill.price:,.2f} | {fill.strategy})")
            return None
        closed_qty = min(abs(lot.qty), abs(signed))
        direction = 1.0 if lot.qty > 0 else -1.0
        pnl = (fill.price - lot.avg_price) * closed_qty * direction - fill.commission
        basis = lot.avg_price * closed_qty
        init_risk = lot.initial_risk_per_share
        if initial_stop is not None:
            init_risk = abs(lot.avg_price - initial_stop)
        r_multiple = round(pnl / (init_risk * closed_qty), 2) if init_risk else None
        if direction > 0:
            best = peak_price if peak_price is not None else max(lot.avg_price, fill.price)
            worst = trough_price if trough_price is not None else min(lot.avg_price, fill.price)
            mfe_val, mae_val = best - lot.avg_price, lot.avg_price - worst
        else:
            best = trough_price if trough_price is not None else min(lot.avg_price, fill.price)
            worst = peak_price if peak_price is not None else max(lot.avg_price, fill.price)
            mfe_val, mae_val = lot.avg_price - best, worst - lot.avg_price
        mfe_pct = mfe_val / lot.avg_price * 100.0 if lot.avg_price > 0 else 0.0
        mae_pct = mae_val / lot.avg_price * 100.0 if lot.avg_price > 0 else 0.0
        mfe_r = mfe_val / init_risk if init_risk and init_risk > 0 else 0.0
        winner_cap = r_multiple / mfe_r if r_multiple is not None and mfe_r > 0 else 0.0
        trade = TradeRecord(symbol=symbol, side="long" if direction > 0 else "short", qty=closed_qty,
            entry_price=round(lot.avg_price, 4), exit_price=round(fill.price, 4), entry_at=lot.entry_ts,
            exit_at=fill.ts, pnl=round(pnl, 2), pnl_pct=round(pnl / basis * 100.0, 3) if basis else 0.0,
            r_multiple=r_multiple, strategy=lot.strategy, exit_reason=fill.reason or "exit", mfe=round(mfe_val, 4),
            mae=round(mae_val, 4), mfe_pct=round(mfe_pct, 4), mae_pct=round(mae_pct, 4), capture_efficiency=round(winner_cap, 4))
        self.trades.append(trade)
        self.realized_pnl += pnl
        self.day_realized_pnl += pnl
        if trade.entry_at.date() == trade.exit_at.date():
            self.day_trades_today += 1
        remainder = lot.qty + signed
        if abs(remainder) < 1e-9:
            del self._lots[symbol]
        elif (remainder > 0) == (lot.qty > 0):
            lot.qty = remainder
        else:
            self._lots[symbol] = _OpenLot(qty=remainder, avg_price=fill.price, entry_ts=fill.ts, strategy=fill.strategy)
        action_str = "SOLD" if trade.side == "long" else "BOUGHT TO COVER"
        pct_str = f"{trade.pnl_pct:+.2f}%" if trade.pnl_pct else "0.0%"
        pnl_text = f"+${pnl:,.2f}" if pnl > 0 else f"-${abs(pnl):,.2f}"
        applog.info("portfolio", f"[{fill.ts}] {action_str} {symbol}: {closed_qty:g} shares @ {fill.price:.2f} -> "
                                f"P&L: {pnl_text} ({pct_str}) ({fill.reason or 'exit'}) | Entered: {trade.entry_at}")
        return trade

    def stats(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {"total_trades": len(self.trades), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(self.trades) * 100.0, 1) if self.trades else 0.0,
            "gross_profit": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "realized_pnl": round(self.realized_pnl, 2), "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0, "day_trades_today": self.day_trades_today}
