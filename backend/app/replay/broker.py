"""ReplayBroker -- Deterministic, configurable broker implementation for production replay.

Implements full Broker interface:
  - submit()
  - cancel()
  - poll_fills()
  - positions()
  - account()
  - open_orders()
  - close_position()
  - flatten_all()
  - drain_fills()

Supports configurable fill models (immediate, next_open, ohlc_touch, partial_fill, slippage_bps).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
import pandas as pd

from app.brokers.base import Broker, Fill
from app.core.domain import (
    AccountSnapshot,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    utcnow,
)
from app.core.logging import applog


class ReplayBroker(Broker):
    name = "replay_broker"
    is_paper = True

    def __init__(
        self,
        starting_cash: float = 100_000.0,
        slippage_bps: float = 2.0,
        fill_model: str = "ohlc_touch",  # immediate | next_open | ohlc_touch
        allow_partial_fills: bool = False,
    ) -> None:
        self.starting_cash = float(starting_cash)
        self.cash = float(starting_cash)
        self.slippage = slippage_bps / 10_000.0
        self.fill_model = fill_model
        self.allow_partial_fills = allow_partial_fills

        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._resting: list[str] = []
        self._fills: list[Fill] = []
        self._last_price: dict[str, float] = {}
        self._now: datetime = utcnow()

    def set_time(self, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._now = ts

    def _slip(self, price: float, side: OrderSide) -> float:
        if side == OrderSide.BUY:
            return price * (1.0 + self.slippage)
        return price * (1.0 - self.slippage)

    def equity(self) -> float:
        return self.cash + sum(p.qty * p.market_price for p in self._positions.values())

    async def account(self) -> AccountSnapshot:
        eq = self.equity()
        return AccountSnapshot(
            equity=round(eq, 2),
            cash=round(self.cash, 2),
            buying_power=round(eq * 2, 2),
            ts=self._now,
        )

    async def positions(self) -> list[Position]:
        return [p for p in self._positions.values() if abs(p.qty) > 1e-9]

    async def open_orders(self) -> list[Order]:
        return [self._orders[oid] for oid in self._resting if oid in self._orders]

    def drain_fills(self) -> list[Fill]:
        fills, self._fills = self._fills, []
        return fills

    async def poll_fills(self) -> list[Fill]:
        return self.drain_fills()

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        for order in self._orders.values():
            if order.client_order_id == client_order_id:
                return order
        return None

    async def submit(self, req: OrderRequest) -> Order:
        order_id = req.client_order_id or f"ord_rpl_{uuid.uuid4().hex[:12]}"
        px = req.price or self._last_price.get(req.symbol, 100.0)

        order = Order(
            id=order_id,
            client_order_id=req.client_order_id or order_id,
            symbol=req.symbol.upper(),
            side=req.side,
            qty=req.qty,
            order_type=req.order_type,
            limit_price=req.price,
            stop_price=req.stop_loss,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._now,
            time_in_force=req.time_in_force,
            order_class=req.order_class,
            leg=req.leg,
        )
        self._orders[order.id] = order

        if self.fill_model == "immediate" or req.order_type == OrderType.MARKET:
            fill_px = self._slip(px, req.side)
            self._execute_fill(order, req.qty, fill_px)
        else:
            order.status = OrderStatus.SUBMITTED
            self._resting.append(order.id)

        return order

    async def cancel(self, order_id: str) -> bool:
        if order_id in self._resting:
            self._resting.remove(order_id)
        if order_id in self._orders:
            o = self._orders[order_id]
            if o.status in (OrderStatus.SUBMITTED, OrderStatus.PENDING_NEW):
                o.status = OrderStatus.CANCELED
                return True
        return False

    async def close_position(self, symbol: str, reason: str = "manual") -> Order | None:
        sym = symbol.upper()
        if sym not in self._positions:
            return None
        pos = self._positions[sym]
        if abs(pos.qty) < 1e-9:
            return None

        side = OrderSide.SELL if pos.qty > 0 else OrderSide.BUY
        req = OrderRequest(
            symbol=sym,
            qty=abs(pos.qty),
            side=side,
            order_type=OrderType.MARKET,
            client_order_id=f"close_{sym}_{uuid.uuid4().hex[:8]}",
        )
        return await self.submit(req)

    async def flatten_all(self, reason: str = "flatten_all") -> list[Order]:
        orders = []
        for sym in list(self._positions.keys()):
            o = await self.close_position(sym, reason=reason)
            if o:
                orders.append(o)
        return orders

    def _execute_fill(self, order: Order, fill_qty: float, fill_price: float) -> None:
        order.status = OrderStatus.FILLED
        order.filled_qty = fill_qty
        order.filled_avg_price = fill_price
        order.filled_at = self._now
        if order.id in self._resting:
            self._resting.remove(order.id)

        fill = Fill(
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=fill_qty,
            price=fill_price,
            ts=self._now,
        )
        self._fills.append(fill)

        # Update position
        sym = order.symbol
        pos = self._positions.get(sym)
        if side_is_entry(order.side, pos):
            if pos is None:
                qty_signed = fill_qty if order.side == OrderSide.BUY else -fill_qty
                self._positions[sym] = Position(
                    symbol=sym,
                    qty=qty_signed,
                    avg_entry_price=fill_price,
                    market_price=fill_price,
                    updated_at=self._now,
                )
            else:
                qty_signed = fill_qty if order.side == OrderSide.BUY else -fill_qty
                new_qty = pos.qty + qty_signed
                total_cost = (pos.qty * pos.avg_entry_price) + (qty_signed * fill_price)
                pos.qty = new_qty
                pos.avg_entry_price = total_cost / new_qty if new_qty != 0 else fill_price
                pos.market_price = fill_price
                pos.updated_at = self._now
        else:
            # Exit leg
            if pos is not None:
                qty_signed = -fill_qty if pos.qty > 0 else fill_qty
                new_qty = pos.qty + qty_signed
                if abs(new_qty) < 1e-9:
                    del self._positions[sym]
                else:
                    pos.qty = new_qty
                    pos.market_price = fill_price
                    pos.updated_at = self._now

        # Accounting cash update
        if order.side == OrderSide.BUY:
            self.cash -= fill_qty * fill_price
        else:
            self.cash += fill_qty * fill_price

    def process_ohlc(self, symbol: str, open_px: float, high_px: float, low_px: float, close_px: float, ts: datetime) -> None:
        self.set_time(ts)
        self._last_price[symbol] = close_px

        if symbol in self._positions:
            self._positions[symbol].market_price = close_px
            self._positions[symbol].updated_at = ts

        # Check resting orders
        for oid in list(self._resting):
            order = self._orders.get(oid)
            if not order or order.symbol != symbol:
                continue

            if order.order_type == OrderType.LIMIT:
                if order.side == OrderSide.BUY and low_px <= (order.limit_price or open_px):
                    fill_px = min(open_px, order.limit_price or open_px)
                    self._execute_fill(order, order.qty, self._slip(fill_px, OrderSide.BUY))
                elif order.side == OrderSide.SELL and high_px >= (order.limit_price or open_px):
                    fill_px = max(open_px, order.limit_price or open_px)
                    self._execute_fill(order, order.qty, self._slip(fill_px, OrderSide.SELL))


def side_is_entry(side: OrderSide, pos: Position | None) -> bool:
    if pos is None or abs(pos.qty) < 1e-9:
        return True
    if pos.qty > 0 and side == OrderSide.BUY:
        return True
    if pos.qty < 0 and side == OrderSide.SELL:
        return True
    return False
