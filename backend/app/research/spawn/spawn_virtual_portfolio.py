"""Stateful Virtual Portfolio Module for Spawned Child Policies.

Maintains isolated cash, equity, virtual open positions, trailing stops,
realized R, MFE/MAE tracking, and drawdown accounting for child variants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        if not self.trailing_stop:
            self.trailing_stop = self.initial_stop

    @property
    def initial_risk_per_share(self) -> float:
        r = abs(self.entry_price - self.initial_stop)
        return r if r > 0 else 1.0


class SpawnVirtualPortfolio:
    def __init__(self, policy_id: str, seed_capital: float = 10000.0, risk_config: dict[str, Any] | None = None) -> None:
        self.policy_id = policy_id
        self.seed_capital = seed_capital
        self.cash = seed_capital
        self.equity = seed_capital
        self.peak_equity = seed_capital
        self.risk_config = risk_config or {}
        
        self.risk_per_trade_pct = float(self.risk_config.get("risk_per_trade_pct", 1.0))
        self.max_position_pct = float(self.risk_config.get("max_position_pct", 25.0))
        self.max_open_positions = int(self.risk_config.get("max_open_positions", 2))

        self.positions: dict[str, VirtualPosition] = {}
        self.pending_orders: list[dict[str, Any]] = []
        self.daily_pnl: float = 0.0
        self.realized_r: float = 0.0
        self.trade_count: int = 0
        self.closed_trades: list[dict[str, Any]] = []

    def get_drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        dd = (self.peak_equity - self.equity) / self.peak_equity * 100.0
        return max(0.0, dd)

    def can_open_position(self, symbol: str) -> bool:
        if symbol.upper() in self.positions:
            return False
        if len(self.positions) >= self.max_open_positions:
            return False
        return True

    def calculate_position_size(self, price: float, stop_loss: float) -> float:
        risk_per_share = abs(price - stop_loss)
        if risk_per_share <= 0:
            return 0.0
        risk_amt = self.equity * (self.risk_per_trade_pct / 100.0)
        qty_risk = risk_amt / risk_per_share
        max_notional = self.equity * (self.max_position_pct / 100.0)
        qty_cap = max_notional / price
        return max(1.0, round(min(qty_risk, qty_cap), 4))

    def open_virtual_position(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        stop_loss: float,
        take_profit: float | None,
        now: datetime,
        order_id: str = ""
    ) -> VirtualPosition | None:
        sym = symbol.upper()
        if not self.can_open_position(sym):
            return None
        
        pos = VirtualPosition(
            symbol=sym,
            side=side.lower(),
            qty=qty,
            entry_price=price,
            entry_at=now,
            initial_stop=stop_loss,
            initial_target=take_profit,
            virtual_order_id=order_id
        )
        self.positions[sym] = pos
        return pos

    def update_price_and_check_exits(self, symbol: str, current_price: float, now: datetime) -> dict[str, Any] | None:
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if not pos:
            return None

        # Update MFE / MAE bounds
        if current_price > pos.peak_price:
            pos.peak_price = current_price
        if current_price < pos.trough_price or pos.trough_price == 0:
            pos.trough_price = current_price

        # Check exits (Stop Loss or Take Profit)
        exit_triggered = False
        exit_reason = ""
        exit_price = current_price

        if pos.side == "long":
            if current_price <= pos.trailing_stop:
                exit_triggered = True
                exit_reason = "STOP_LOSS"
                exit_price = pos.trailing_stop
            elif pos.initial_target and current_price >= pos.initial_target:
                exit_triggered = True
                exit_reason = "TAKE_PROFIT"
                exit_price = pos.initial_target
        else:  # short
            if current_price >= pos.trailing_stop:
                exit_triggered = True
                exit_reason = "STOP_LOSS"
                exit_price = pos.trailing_stop
            elif pos.initial_target and current_price <= pos.initial_target:
                exit_triggered = True
                exit_reason = "TAKE_PROFIT"
                exit_price = pos.initial_target

        if exit_triggered:
            return self.close_virtual_position(sym, exit_price, exit_reason, now)
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
        self.equity += pnl
        self.daily_pnl += pnl
        self.realized_r += realized_r
        self.trade_count += 1
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        trade_rec = {
            "policy_id": self.policy_id,
            "symbol": sym,
            "side": pos.side,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "entry_at": pos.entry_at.isoformat(),
            "exit_at": now.isoformat(),
            "pnl": pnl,
            "realized_r": realized_r,
            "mfe_r": mfe_r,
            "mae_r": mae_r,
            "exit_reason": reason,
        }
        self.closed_trades.append(trade_rec)
        return trade_rec



    def to_checkpoint_dict(self) -> dict[str, Any]:
        pos_dict = {}
        for sym, pos in self.positions.items():
            pos_dict[sym] = {
                "symbol": pos.symbol,
                "side": pos.side,
                "qty": pos.qty,
                "entry_price": pos.entry_price,
                "entry_at": pos.entry_at.isoformat(),
                "initial_stop": pos.initial_stop,
                "initial_target": pos.initial_target,
                "trailing_stop": pos.trailing_stop,
                "peak_price": pos.peak_price,
                "trough_price": pos.trough_price,
                "virtual_order_id": pos.virtual_order_id,
            }
        return {
            "policy_id": self.policy_id,
            "cash": self.cash,
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "positions": pos_dict,
            "pending_orders": self.pending_orders,
            "daily_pnl": self.daily_pnl,
            "realized_r": self.realized_r,
            "trade_count": self.trade_count,
            "drawdown": self.get_drawdown_pct(),
        }

    def restore_from_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        self.cash = float(checkpoint.get("cash", self.seed_capital))
        self.equity = float(checkpoint.get("equity", self.seed_capital))
        self.peak_equity = float(checkpoint.get("peak_equity", self.equity))
        self.daily_pnl = float(checkpoint.get("daily_pnl", 0.0))
        self.realized_r = float(checkpoint.get("realized_r", 0.0))
        self.trade_count = int(checkpoint.get("trade_count", 0))

        pos_dict = checkpoint.get("positions") or {}
        self.positions.clear()
        for sym, p in pos_dict.items():
            entry_at_raw = p.get("entry_at")
            entry_at = datetime.fromisoformat(entry_at_raw) if isinstance(entry_at_raw, str) else datetime.now(timezone.utc)
            self.positions[sym.upper()] = VirtualPosition(
                symbol=sym.upper(),
                side=p.get("side", "long"),
                qty=float(p.get("qty", 1.0)),
                entry_price=float(p.get("entry_price", 0.0)),
                entry_at=entry_at,
                initial_stop=float(p.get("initial_stop", 0.0)),
                initial_target=p.get("initial_target"),
                trailing_stop=p.get("trailing_stop"),
                peak_price=float(p.get("peak_price", p.get("entry_price", 0.0))),
                trough_price=float(p.get("trough_price", p.get("entry_price", 0.0))),
                virtual_order_id=p.get("virtual_order_id", ""),
            )
