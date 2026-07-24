"""Stateful Virtual Portfolio Module for Spawned Child Policies.

Maintains isolated cash, marked equity, virtual open positions, OHLC trailing stops,
realized/unrealized P&L, MFE/MAE tracking, and drawdown accounting for child variants.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any


@dataclass
class VirtualPosition:
    symbol: str
    side: str  # "long" | "short"
    qty: float
    entry_price: float
    entry_at: datetime
    initial_stop: float
    initial_target: float | None = None
    trailing_stop: float | None = None
    peak_price: float = 0.0
    trough_price: float = 0.0
    virtual_order_id: str = ""

    def __post_init__(self):
        if not self.peak_price:
            self.peak_price = self.entry_price
        if not self.trough_price:
            self.trough_price = self.entry_price
        if self.trailing_stop is None:
            self.trailing_stop = self.initial_stop

    @property
    def initial_risk_per_share(self) -> float:
        r = abs(self.entry_price - self.initial_stop)
        return r if r > 0 else 1.0

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "long":
            return (current_price - self.entry_price) * self.qty
        return (self.entry_price - current_price) * self.qty


class SpawnVirtualPortfolio:
    def __init__(self, policy_id: str, seed_capital: float = 10000.0, risk_config: dict[str, Any] | None = None) -> None:
        self.policy_id = policy_id
        self.seed_capital = seed_capital
        self.cash = seed_capital
        self.realized_equity = seed_capital
        self.marked_equity = seed_capital
        self.peak_equity = seed_capital
        self.risk_config = risk_config or {}

        self.risk_per_trade_pct = float(self.risk_config.get("risk_per_trade_pct", 1.0))
        self.max_position_pct = float(self.risk_config.get("max_position_pct", 25.0))
        self.max_open_positions = int(self.risk_config.get("max_open_positions", 2))
        self.allow_fractional = bool(self.risk_config.get("allow_fractional", False))

        self.positions: dict[str, VirtualPosition] = {}
        self.last_prices: dict[str, float] = {}
        self.pending_orders: list[dict[str, Any]] = []
        self.daily_pnl: float = 0.0
        self.realized_r: float = 0.0
        self.trade_count: int = 0
        self.closed_trades: list[dict[str, Any]] = []

    def mark_to_market(self, current_prices: dict[str, float]) -> float:
        """Recalculates marked equity across all positions using persistent last prices."""
        if current_prices:
            self.last_prices.update(current_prices)

        unrealized = 0.0
        for sym, pos in self.positions.items():
            price = self.last_prices.get(sym, pos.entry_price)
            unrealized += pos.unrealized_pnl(price)

        self.marked_equity = self.cash + unrealized
        if self.marked_equity > self.peak_equity:
            self.peak_equity = self.marked_equity
        return self.marked_equity

    def get_drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        dd = (self.peak_equity - self.marked_equity) / self.peak_equity * 100.0
        return max(0.0, dd)

    def can_open_position(self, symbol: str) -> bool:
        if symbol.upper() in self.positions:
            return False
        return len(self.positions) < self.max_open_positions

    def calculate_position_size(self, price: float, stop_loss: float) -> float:
        risk_per_share = abs(price - stop_loss)
        if price <= 0 or risk_per_share <= 0:
            return 0.0
        risk_amt = self.marked_equity * (self.risk_per_trade_pct / 100.0)
        qty_risk = risk_amt / risk_per_share
        max_notional = self.marked_equity * (self.max_position_pct / 100.0)
        qty_cap = max_notional / price

        raw_qty = min(qty_risk, qty_cap)
        if self.allow_fractional:
            return max(0.0, round(raw_qty, 4))
        qty_int = math.floor(raw_qty)
        return float(qty_int) if qty_int >= 1 else 0.0

    def open_virtual_position(self, symbol: str, side: str, qty: float, price: float, stop_loss: float,
                              take_profit: float | None, now: datetime, order_id: str = "") -> VirtualPosition | None:
        sym = symbol.upper()
        if not self.can_open_position(sym) or qty <= 0:
            return None
        pos = VirtualPosition(sym, side.lower(), qty, price, now, stop_loss, take_profit, virtual_order_id=order_id)
        self.positions[sym] = pos
        self.last_prices[sym] = price
        self.mark_to_market({sym: price})
        return pos

    def update_ohlc_and_check_exits(self, symbol: str, open_px: float, high_px: float, low_px: float,
                                    close_px: float, now: datetime) -> dict[str, Any] | None:
        """Apply one OHLC bar. If stop and target both touch, stop wins conservatively."""
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if not pos:
            return None

        effective_stop = pos.trailing_stop if pos.trailing_stop is not None else pos.initial_stop
        exit_triggered = False
        exit_reason = ""
        exit_price = close_px

        # OHLC cannot reveal the ordering of stop/target touches inside a bar. Use a
        # deterministic conservative policy: stop takes precedence when both touch.
        if pos.side == "long":
            if low_px <= effective_stop:
                exit_triggered, exit_reason, exit_price = True, "STOP_LOSS", effective_stop
            elif pos.initial_target and high_px >= pos.initial_target:
                exit_triggered, exit_reason, exit_price = True, "TAKE_PROFIT", pos.initial_target
        else:
            if high_px >= effective_stop:
                exit_triggered, exit_reason, exit_price = True, "STOP_LOSS", effective_stop
            elif pos.initial_target and low_px <= pos.initial_target:
                exit_triggered, exit_reason, exit_price = True, "TAKE_PROFIT", pos.initial_target

        if exit_triggered:
            return self.close_virtual_position(sym, exit_price, exit_reason, now)

        if high_px > pos.peak_price:
            pos.peak_price = high_px
        if low_px < pos.trough_price or pos.trough_price == 0:
            pos.trough_price = low_px

        trail_mult = float(self.risk_config.get("trailing_stop_atr_mult", 0.0))
        if trail_mult > 0:
            risk_dist = pos.initial_risk_per_share
            if pos.side == "long" and pos.peak_price > pos.entry_price:
                new_trail = pos.peak_price - (risk_dist * trail_mult)
                if pos.trailing_stop is None or new_trail > pos.trailing_stop:
                    pos.trailing_stop = new_trail
            elif pos.side == "short" and pos.trough_price < pos.entry_price:
                new_trail = pos.trough_price + (risk_dist * trail_mult)
                if pos.trailing_stop is None or new_trail < pos.trailing_stop:
                    pos.trailing_stop = new_trail

        self.mark_to_market({sym: close_px})
        return None

    def close_virtual_position(self, symbol: str, exit_price: float, reason: str, now: datetime) -> dict[str, Any]:
        sym = symbol.upper()
        pos = self.positions.pop(sym)
        pnl = (exit_price - pos.entry_price) * pos.qty if pos.side == "long" else (pos.entry_price - exit_price) * pos.qty
        risk_per_share = pos.initial_risk_per_share
        realized_r = (exit_price - pos.entry_price) / risk_per_share if pos.side == "long" else (pos.entry_price - exit_price) / risk_per_share
        mfe_r = (pos.peak_price - pos.entry_price) / risk_per_share if pos.side == "long" else (pos.entry_price - pos.trough_price) / risk_per_share
        mae_r = (pos.entry_price - pos.trough_price) / risk_per_share if pos.side == "long" else (pos.peak_price - pos.entry_price) / risk_per_share

        self.cash += pnl
        self.realized_equity += pnl
        self.daily_pnl += pnl
        self.realized_r += realized_r
        self.trade_count += 1
        self.last_prices[sym] = exit_price
        self.mark_to_market({sym: exit_price})

        trade_rec = {
            "policy_id": self.policy_id, "symbol": sym, "side": pos.side, "qty": pos.qty,
            "entry_price": pos.entry_price, "exit_price": exit_price,
            "entry_at": pos.entry_at.isoformat(), "exit_at": now.isoformat(), "pnl": pnl,
            "realized_r": realized_r, "mfe_r": mfe_r, "mae_r": mae_r, "exit_reason": reason,
        }
        self.closed_trades.append(trade_rec)
        return trade_rec

    def to_checkpoint_dict(self) -> dict[str, Any]:
        pos_dict = {}
        for sym, pos in self.positions.items():
            pos_dict[sym] = {
                "symbol": pos.symbol, "side": pos.side, "qty": pos.qty, "entry_price": pos.entry_price,
                "entry_at": pos.entry_at.isoformat(), "initial_stop": pos.initial_stop,
                "initial_target": pos.initial_target, "trailing_stop": pos.trailing_stop,
                "peak_price": pos.peak_price, "trough_price": pos.trough_price,
                "virtual_order_id": pos.virtual_order_id,
            }
        return {
            "policy_id": self.policy_id, "cash": self.cash, "equity": self.marked_equity,
            "peak_equity": self.peak_equity, "positions": pos_dict, "last_prices": dict(self.last_prices),
            "pending_orders": self.pending_orders, "daily_pnl": self.daily_pnl,
            "realized_r": self.realized_r, "trade_count": self.trade_count, "drawdown": self.get_drawdown_pct(),
        }

    def restore_from_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        self.cash = float(checkpoint.get("cash", self.seed_capital))
        self.marked_equity = float(checkpoint.get("equity", self.seed_capital))
        self.realized_equity = self.cash
        self.peak_equity = float(checkpoint.get("peak_equity", self.marked_equity))
        self.daily_pnl = float(checkpoint.get("daily_pnl", 0.0))
        self.realized_r = float(checkpoint.get("realized_r", 0.0))
        self.trade_count = int(checkpoint.get("trade_count", 0))
        self.last_prices = {str(k).upper(): float(v) for k, v in (checkpoint.get("last_prices") or {}).items()}

        pos_dict = checkpoint.get("positions") or {}
        restored: dict[str, VirtualPosition] = {}
        for sym, p in pos_dict.items():
            entry_at_raw = p.get("entry_at")
            if isinstance(entry_at_raw, str):
                try:
                    entry_at = datetime.fromisoformat(entry_at_raw)
                except ValueError as exc:
                    raise ValueError(f"Invalid checkpoint entry_at for {sym}: {entry_at_raw!r}") from exc
            elif isinstance(entry_at_raw, datetime):
                entry_at = entry_at_raw
            else:
                raise ValueError(f"Missing checkpoint entry_at for {sym}")
            if entry_at.tzinfo is None:
                raise ValueError(f"Naive checkpoint entry_at for {sym}: {entry_at_raw!r}")

            restored[sym.upper()] = VirtualPosition(
                symbol=sym.upper(), side=p.get("side", "long"), qty=float(p.get("qty", 1.0)),
                entry_price=float(p.get("entry_price", 0.0)), entry_at=entry_at,
                initial_stop=float(p.get("initial_stop", 0.0)), initial_target=p.get("initial_target"),
                trailing_stop=p.get("trailing_stop"), peak_price=float(p.get("peak_price", p.get("entry_price", 0.0))),
                trough_price=float(p.get("trough_price", p.get("entry_price", 0.0))),
                virtual_order_id=p.get("virtual_order_id", ""),
            )
        self.positions = restored
        for sym, pos in self.positions.items():
            self.last_prices.setdefault(sym, pos.entry_price)
