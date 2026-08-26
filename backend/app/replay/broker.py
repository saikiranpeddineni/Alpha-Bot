"""Deterministic Broker implementation used by production replay."""
from __future__ import annotations

from datetime import UTC, datetime

from app.brokers.base import Broker, Fill
from app.core.domain import AccountSnapshot, Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position


class ReplayBroker(Broker):
    name, is_paper = "replay_broker", True

    def __init__(self, starting_cash: float = 100_000.0, slippage_bps: float = 0.0,
                 fill_model: str = "ohlc_touch", allow_partial_fills: bool = False,
                 volume_constrained: bool = False, max_volume_pct: float = 0.05) -> None:
        if fill_model not in {"immediate", "next_open", "ohlc_touch"}: raise ValueError(f"Unsupported fill model {fill_model}")
        self.starting_cash = self.cash = float(starting_cash); self.slippage = float(slippage_bps) / 10_000.0
        self.fill_model, self.allow_partial_fills = fill_model, bool(allow_partial_fills)
        self.volume_constrained, self.max_volume_pct = bool(volume_constrained), float(max_volume_pct)
        self._positions: dict[str, Position] = {}; self._orders: dict[str, Order] = {}; self._resting: list[str] = []
        self._fills: list[Fill] = []; self._last_price: dict[str, float] = {}; self._now = datetime(1970,1,1,tzinfo=UTC)
        self._seq = 0; self._ledger = None

    def attach_ledger(self, ledger) -> None: self._ledger = ledger
    async def connect(self) -> None: return None
    def set_time(self, ts: datetime) -> None: self._now = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    def _id(self, prefix: str) -> str: self._seq += 1; return f"{prefix}_{self._seq:08d}"
    def _slip(self, px: float, side: OrderSide) -> float: return px * (1 + self.slippage) if side == OrderSide.BUY else px * (1 - self.slippage)
    def equity(self) -> float: return self.cash + sum(p.qty * p.market_price for p in self._positions.values())

    async def account(self) -> AccountSnapshot:
        eq = self.equity(); return AccountSnapshot(equity=round(eq,8), cash=round(self.cash,8), buying_power=round(eq*2,8), ts=self._now)
    async def positions(self) -> list[Position]: return [p for _,p in sorted(self._positions.items()) if abs(p.qty)>1e-12]
    async def open_orders(self) -> list[Order]: return [self._orders[x] for x in self._resting if x in self._orders]
    async def get_order_by_client_id(self, client_order_id: str) -> Order | None: return next((o for o in self._orders.values() if o.client_order_id==client_order_id),None)
    async def is_shortable(self, symbol: str) -> bool: return True
    def drain_fills(self) -> list[Fill]: fills,self._fills=self._fills,[]; return fills
    async def poll_fills(self) -> list[Fill]: return list(self._fills)

    async def submit(self, req: OrderRequest) -> Order:
        oid = req.client_order_id or self._id("ord_rpl")
        if oid in self._orders: return self._orders[oid]
        order = Order(id=oid, client_order_id=oid, symbol=req.symbol.upper(), side=req.side, qty=float(req.qty), order_type=req.order_type,
                      limit_price=req.limit_price, stop_price=req.stop_price, stop_loss=req.stop_loss, take_profit=req.take_profit,
                      status=OrderStatus.SUBMITTED, submitted_at=self._now, strategy=req.strategy, reason=req.reason,
                      signal_timestamp_utc=req.signal_timestamp_utc, decision_bar_timestamp_utc=req.decision_bar_timestamp_utc)
        self._orders[oid]=order
        if self._ledger: self._ledger.record_event("ORDER_SUBMITTED", self._now, order.symbol, order.model_dump(mode="json"))
        if self.fill_model=="immediate" or req.order_type==OrderType.MARKET:
            px=self._last_price.get(order.symbol)
            if px is None: raise RuntimeError(f"No as-of price for MARKET order {order.symbol}")
            self._execute_fill(order, order.qty, self._slip(px,order.side))
        else: self._resting.append(oid)
        return order

    async def cancel(self, order_id: str) -> bool:
        order=self._orders.get(order_id)
        if order is None: return False
        if order_id in self._resting: self._resting.remove(order_id)
        if order.status in (OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            order.status=OrderStatus.CANCELED; return True
        return False
    async def cancel_all(self) -> None:
        for oid in list(self._resting): await self.cancel(oid)

    async def close_position(self, symbol: str, reason: str="manual", qty: float | None = None) -> Order | None:
        pos=self._positions.get(symbol.upper())
        if pos is None or abs(pos.qty)<=1e-12: return None
        side=OrderSide.SELL if pos.qty>0 else OrderSide.BUY
        close_qty = min(abs(pos.qty), qty) if qty is not None and qty > 0 else abs(pos.qty)
        return await self.submit(OrderRequest(symbol=symbol.upper(),qty=close_qty,side=side,order_type=OrderType.MARKET,
                                              client_order_id=self._id(f"close_{symbol.upper()}"),strategy=pos.strategy,reason=reason))
    async def flatten_all(self, reason: str="flatten") -> list[Order]:
        out=[]
        for symbol in sorted(list(self._positions)):
            order=await self.close_position(symbol,reason)
            if order: out.append(order)
        return out

    @staticmethod
    def _is_protective_exit(order: Order) -> bool:
        strategy = str(getattr(order, "strategy", "") or "").lower()
        reason = str(getattr(order, "reason", "") or "").lower()
        client_id = str(getattr(order, "client_order_id", "") or "").lower()
        return strategy == "emergency_protection" or client_id.startswith("protect-") or "protective stop" in reason

    def _cancel_stale_protection(self, symbol: str) -> None:
        """Cancel replay-only protective exits once their protected position is flat.

        A broker-side protective stop is position-reducing protection, not permission to
        create a new position after another exit has already flattened the symbol.  Without
        this replay broker guard a stale stop can fire on a later bar and manufacture the
        opposite position, which is not an economic trade from the production strategy.
        """
        symbol = symbol.upper()
        for oid in list(self._resting):
            order = self._orders.get(oid)
            if order is None or order.symbol != symbol or not self._is_protective_exit(order):
                continue
            self._resting.remove(oid)
            if order.status in (OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
                order.status = OrderStatus.CANCELED
            if self._ledger:
                self._ledger.record_event("ORDER_CANCELED", self._now, symbol, {
                    "order_id": order.id,
                    "reason": "protected position already flat",
                    "strategy": order.strategy,
                })

    def _execute_fill(self, order: Order, qty: float, price: float) -> None:
        qty=min(float(qty),order.remaining_qty)
        if qty<=0:return
        old_filled=float(order.filled_qty or 0); new_filled=old_filled+qty
        order.filled_avg_price=price if old_filled==0 else ((order.filled_avg_price or 0)*old_filled+price*qty)/new_filled
        order.filled_qty=new_filled; order.filled_at=self._now
        order.status=OrderStatus.FILLED if order.remaining_qty<=1e-12 else OrderStatus.PARTIALLY_FILLED
        if order.status==OrderStatus.FILLED and order.id in self._resting:self._resting.remove(order.id)
        fill=Fill(order_id=order.id,symbol=order.symbol,side=order.side,qty=qty,price=price,strategy=order.strategy,reason=order.reason,ts=self._now,
                  signal_timestamp_utc=order.signal_timestamp_utc,decision_bar_timestamp_utc=order.decision_bar_timestamp_utc,
                  broker_fill_key=f"{order.id}:{new_filled:.8f}",cumulative_qty=new_filled,broker_mode="replay")
        self._fills.append(fill)
        if self._ledger:self._ledger.record_event("FILL_EXECUTED",self._now,order.symbol,{"order_id":order.id,"side":str(order.side),"qty":qty,"price":price,"strategy":order.strategy,"reason":order.reason})
        sym=order.symbol; pos=self._positions.get(sym); signed=qty if order.side==OrderSide.BUY else -qty
        old_qty=pos.qty if pos else 0.0; new_qty=old_qty+signed
        if pos is None or abs(old_qty)<=1e-12:
            if abs(new_qty)>1e-12:self._positions[sym]=Position(symbol=sym,qty=new_qty,avg_entry_price=price,market_price=price,stop_loss=order.stop_loss,take_profit=order.take_profit,strategy=order.strategy,opened_at=self._now)
        elif old_qty*signed>0:
            basis=abs(old_qty)*pos.avg_entry_price+abs(signed)*price; pos.qty=new_qty; pos.avg_entry_price=basis/abs(new_qty); pos.market_price=price
        elif abs(new_qty)<=1e-12: del self._positions[sym]
        elif old_qty*new_qty>0: pos.qty=new_qty; pos.market_price=price
        else:self._positions[sym]=Position(symbol=sym,qty=new_qty,avg_entry_price=price,market_price=price,strategy=order.strategy,opened_at=self._now)
        self.cash += -qty*price if order.side==OrderSide.BUY else qty*price
        if sym not in self._positions:
            self._cancel_stale_protection(sym)

    def process_ohlc(self, symbol: str, open_px: float, high_px: float, low_px: float, close_px: float, ts: datetime, volume: float = 0.0) -> None:
        symbol = symbol.upper(); self.set_time(ts); self._last_price[symbol] = close_px
        if symbol in self._positions: self._positions[symbol].market_price = close_px
        for oid in list(self._resting):
            order = self._orders.get(oid)
            if order is None or order.symbol != symbol: continue
            # A protective exit is reduce-only by intent. If another exit flattened the
            # position before this bar, cancel it rather than allowing a synthetic reversal.
            if self._is_protective_exit(order) and symbol not in self._positions:
                self._cancel_stale_protection(symbol)
                continue
            fill_px = None
            if order.order_type == OrderType.LIMIT:
                limit = float(order.limit_price)
                if order.side == OrderSide.BUY and low_px <= limit: fill_px = min(open_px, limit)
                elif order.side == OrderSide.SELL and high_px >= limit: fill_px = max(open_px, limit)
            elif order.order_type == OrderType.STOP:
                stop = float(order.stop_price)
                if order.side == OrderSide.BUY and high_px >= stop: fill_px = max(open_px, stop)
                elif order.side == OrderSide.SELL and low_px <= stop: fill_px = min(open_px, stop)
            elif order.order_type == OrderType.STOP_LIMIT:
                stop, limit = float(order.stop_price), float(order.limit_price)
                order.stop_triggered = order.stop_triggered or (order.side == OrderSide.BUY and high_px >= stop) or (order.side == OrderSide.SELL and low_px <= stop)
                if order.stop_triggered:
                    if order.side == OrderSide.BUY and low_px <= limit: fill_px = min(max(open_px, stop), limit)
                    elif order.side == OrderSide.SELL and high_px >= limit: fill_px = max(min(open_px, stop), limit)
            elif self.fill_model == "next_open": fill_px = open_px
            if fill_px is not None:
                qty_to_fill = order.remaining_qty
                if self.volume_constrained and volume > 0:
                    max_fill = float(volume) * self.max_volume_pct
                    qty_to_fill = min(qty_to_fill, max_fill)
                if qty_to_fill > 0:
                    self._execute_fill(order, qty_to_fill, self._slip(fill_px, order.side))
