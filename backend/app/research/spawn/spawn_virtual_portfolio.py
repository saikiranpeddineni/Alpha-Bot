"""Stateful virtual portfolio for spawned child policies."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any


@dataclass
class VirtualPosition:
    symbol: str
    side: str
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
        self.peak_price = self.peak_price or self.entry_price
        self.trough_price = self.trough_price or self.entry_price
        if self.trailing_stop is None:
            self.trailing_stop = self.initial_stop

    @property
    def initial_risk_per_share(self):
        risk = abs(self.entry_price - self.initial_stop)
        return risk if risk > 0 else 1.0

    def unrealized_pnl(self, price):
        return ((price - self.entry_price) if self.side == "long" else (self.entry_price - price)) * self.qty


class SpawnVirtualPortfolio:
    MAX_DURABLE_WATERMARKS = 10000

    def __init__(self, policy_id, seed_capital=10000.0, risk_config=None):
        self.policy_id = policy_id
        self.seed_capital = seed_capital
        self.cash = seed_capital
        self.realized_equity = seed_capital
        self.marked_equity = seed_capital
        self.peak_equity = seed_capital
        self.max_drawdown_pct = 0.0
        self.risk_config = risk_config or {}
        self.risk_per_trade_pct = float(self.risk_config.get("risk_per_trade_pct", 1.0))
        self.max_position_pct = float(self.risk_config.get("max_position_pct", 25.0))
        self.max_open_positions = int(self.risk_config.get("max_open_positions", 2))
        self.allow_fractional = bool(self.risk_config.get("allow_fractional", False))
        self.positions = {}
        self.last_prices = {}
        self.pending_orders = []
        self.daily_pnl = 0.0
        self.realized_r = 0.0
        self.trade_count = 0
        self.closed_trades = []
        self.processed_watermarks = {}

    def record_processed_watermark(self, key):
        self.processed_watermarks[key] = None
        overflow = len(self.processed_watermarks) - self.MAX_DURABLE_WATERMARKS
        if overflow > 0:
            for old in list(self.processed_watermarks)[:overflow]:
                self.processed_watermarks.pop(old, None)

    def mark_to_market(self, prices):
        if prices:
            self.last_prices.update(prices)
        self.marked_equity = self.cash + sum(
            position.unrealized_pnl(self.last_prices.get(symbol, position.entry_price))
            for symbol, position in self.positions.items()
        )
        self.peak_equity = max(self.peak_equity, self.marked_equity)
        self.max_drawdown_pct = max(self.max_drawdown_pct, self.get_drawdown_pct())
        return self.marked_equity

    def get_drawdown_pct(self):
        return max(0.0, (self.peak_equity - self.marked_equity) / self.peak_equity * 100.0) if self.peak_equity > 0 else 0.0

    def get_max_drawdown_pct(self):
        return self.max_drawdown_pct

    def can_open_position(self, symbol):
        return symbol.upper() not in self.positions and len(self.positions) < self.max_open_positions

    def calculate_position_size(self, price, stop):
        risk = abs(price - stop)
        if price <= 0 or risk <= 0:
            return 0.0
        raw = min(
            self.marked_equity * (self.risk_per_trade_pct / 100.0) / risk,
            self.marked_equity * (self.max_position_pct / 100.0) / price,
        )
        return max(0.0, round(raw, 4)) if self.allow_fractional else (float(math.floor(raw)) if raw >= 1 else 0.0)

    def open_virtual_position(self, symbol, side, qty, price, stop, target, now, order_id=""):
        sym = symbol.upper()
        if not self.can_open_position(sym) or qty <= 0:
            return None
        position = VirtualPosition(sym, side.lower(), qty, price, now, stop, target, virtual_order_id=order_id)
        self.positions[sym] = position
        self.last_prices[sym] = price
        self.mark_to_market({sym: price})
        return position

    def update_ohlc_and_check_exits(
        self,
        symbol,
        o=None,
        h=None,
        l=None,
        c=None,
        now=None,
        **legacy_kwargs,
    ):
        """Apply one causally completed OHLC bar to an open virtual position.

        The effective stop entering the bar is evaluated before any trailing-stop
        update derived from that bar.  Legacy keyword aliases remain accepted so
        older safety fixtures exercise the same implementation rather than fail
        at the call boundary.
        """
        if o is None:
            o = legacy_kwargs.pop("open_px", None)
        if h is None:
            h = legacy_kwargs.pop("high_px", None)
        if l is None:
            l = legacy_kwargs.pop("low_px", None)
        if c is None:
            c = legacy_kwargs.pop("close_px", None)
        if now is None:
            now = legacy_kwargs.pop("ts", None)
        if legacy_kwargs:
            raise TypeError(f"Unexpected OHLC arguments: {sorted(legacy_kwargs)}")
        if any(value is None for value in (o, h, l, c, now)):
            raise ValueError("Complete OHLC values and timestamp are required")

        sym = symbol.upper()
        position = self.positions.get(sym)
        if not position:
            return None

        bar_watermark = f"BAR|{sym}|{now.isoformat()}"
        if bar_watermark in self.processed_watermarks:
            return None

        stop = position.trailing_stop if position.trailing_stop is not None else position.initial_stop
        exit_record = None
        if position.side == "long":
            if l <= stop:
                exit_record = self.close_virtual_position(sym, stop, "STOP_LOSS", now)
            elif position.initial_target and h >= position.initial_target:
                exit_record = self.close_virtual_position(sym, position.initial_target, "TAKE_PROFIT", now)
        else:
            if h >= stop:
                exit_record = self.close_virtual_position(sym, stop, "STOP_LOSS", now)
            elif position.initial_target and l <= position.initial_target:
                exit_record = self.close_virtual_position(sym, position.initial_target, "TAKE_PROFIT", now)

        self.record_processed_watermark(bar_watermark)
        if exit_record is not None:
            return exit_record

        position.peak_price = max(position.peak_price, h)
        position.trough_price = min(position.trough_price, l)
        mult = float(self.risk_config.get("trailing_stop_atr_mult", 0.0))
        risk = position.initial_risk_per_share
        if mult > 0 and position.side == "long" and position.peak_price > position.entry_price:
            position.trailing_stop = max(position.trailing_stop or position.initial_stop, position.peak_price - risk * mult)
        elif mult > 0 and position.side == "short" and position.trough_price < position.entry_price:
            position.trailing_stop = min(position.trailing_stop or position.initial_stop, position.trough_price + risk * mult)
        self.mark_to_market({sym: c})
        return None

    def close_virtual_position(self, symbol, exit_price, reason, now):
        sym = symbol.upper()
        position = self.positions.pop(sym)
        risk = position.initial_risk_per_share
        pnl = ((exit_price - position.entry_price) if position.side == "long" else (position.entry_price - exit_price)) * position.qty
        realized_r = ((exit_price - position.entry_price) if position.side == "long" else (position.entry_price - exit_price)) / risk
        mfe_r = ((position.peak_price - position.entry_price) if position.side == "long" else (position.entry_price - position.trough_price)) / risk
        mae_r = ((position.entry_price - position.trough_price) if position.side == "long" else (position.peak_price - position.entry_price)) / risk
        self.cash += pnl
        self.realized_equity += pnl
        self.daily_pnl += pnl
        self.realized_r += realized_r
        self.trade_count += 1
        self.last_prices[sym] = exit_price
        self.mark_to_market({sym: exit_price})
        record = {
            "policy_id": self.policy_id,
            "symbol": sym,
            "side": position.side,
            "qty": position.qty,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "entry_at": position.entry_at.isoformat(),
            "exit_at": now.isoformat(),
            "pnl": pnl,
            "realized_r": realized_r,
            "mfe_r": mfe_r,
            "mae_r": mae_r,
            "exit_reason": reason,
        }
        self.closed_trades.append(record)
        return record

    def to_checkpoint_dict(self):
        positions = {
            symbol: {
                "symbol": position.symbol,
                "side": position.side,
                "qty": position.qty,
                "entry_price": position.entry_price,
                "entry_at": position.entry_at.isoformat(),
                "initial_stop": position.initial_stop,
                "initial_target": position.initial_target,
                "trailing_stop": position.trailing_stop,
                "peak_price": position.peak_price,
                "trough_price": position.trough_price,
                "virtual_order_id": position.virtual_order_id,
            }
            for symbol, position in self.positions.items()
        }
        envelope = {
            "schema": 3,
            "orders": self.pending_orders,
            "last_prices": dict(self.last_prices),
            "processed_watermarks": list(self.processed_watermarks),
            "max_drawdown_pct": self.max_drawdown_pct,
        }
        return {
            "policy_id": self.policy_id,
            "cash": self.cash,
            "equity": self.marked_equity,
            "peak_equity": self.peak_equity,
            "positions": positions,
            "last_prices": dict(self.last_prices),
            "processed_watermarks": list(self.processed_watermarks),
            "pending_orders": envelope,
            "daily_pnl": self.daily_pnl,
            "realized_r": self.realized_r,
            "trade_count": self.trade_count,
            "drawdown": self.get_drawdown_pct(),
            "max_drawdown_pct": self.max_drawdown_pct,
        }

    def restore_from_checkpoint(self, checkpoint):
        self.cash = float(checkpoint.get("cash", self.seed_capital))
        self.marked_equity = float(checkpoint.get("equity", self.seed_capital))
        self.realized_equity = self.cash
        self.peak_equity = float(checkpoint.get("peak_equity", self.marked_equity))
        self.daily_pnl = float(checkpoint.get("daily_pnl", 0))
        self.realized_r = float(checkpoint.get("realized_r", 0))
        self.trade_count = int(checkpoint.get("trade_count", 0))
        envelope = checkpoint.get("pending_orders") or {}
        durable = envelope if isinstance(envelope, dict) and envelope.get("schema") in (2, 3) else {}
        self.pending_orders = list(durable.get("orders", [])) if durable else (envelope if isinstance(envelope, list) else [])
        prices = checkpoint.get("last_prices") or durable.get("last_prices") or {}
        marks = checkpoint.get("processed_watermarks") or durable.get("processed_watermarks") or []
        self.max_drawdown_pct = float(checkpoint.get("max_drawdown_pct", durable.get("max_drawdown_pct", checkpoint.get("drawdown", 0.0))))
        self.last_prices = {str(key).upper(): float(value) for key, value in prices.items()}
        self.processed_watermarks = {str(key): None for key in marks[-self.MAX_DURABLE_WATERMARKS:]}
        restored = {}
        for sym, payload in (checkpoint.get("positions") or {}).items():
            raw = payload.get("entry_at")
            if isinstance(raw, str):
                try:
                    entry = datetime.fromisoformat(raw)
                except ValueError as exc:
                    raise ValueError(f"Invalid checkpoint entry_at for {sym}: {raw!r}") from exc
            elif isinstance(raw, datetime):
                entry = raw
            else:
                raise ValueError(f"Missing checkpoint entry_at for {sym}")
            if entry.tzinfo is None:
                raise ValueError(f"Naive checkpoint entry_at for {sym}: {raw!r}")
            restored[sym.upper()] = VirtualPosition(
                sym.upper(),
                payload.get("side", "long"),
                float(payload.get("qty", 1)),
                float(payload.get("entry_price", 0)),
                entry,
                float(payload.get("initial_stop", 0)),
                payload.get("initial_target"),
                payload.get("trailing_stop"),
                float(payload.get("peak_price", payload.get("entry_price", 0))),
                float(payload.get("trough_price", payload.get("entry_price", 0))),
                payload.get("virtual_order_id", ""),
            )
        self.positions = restored
        for sym, position in restored.items():
            self.last_prices.setdefault(sym, position.entry_price)
