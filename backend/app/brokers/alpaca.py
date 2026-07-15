"""Alpaca broker adapter (alpaca-py SDK).

Broker order state is polled as an authoritative cumulative execution watermark.
The portfolio layer commits each watermark atomically with its durable accounting
checkpoint, so a process restart replays only the unprocessed quantity.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.brokers.base import Broker, Fill
from app.core.domain import AccountSnapshot, Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position
from app.core.logging import applog

_STATUS_MAP = {
    "new": OrderStatus.SUBMITTED, "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED, "partially_filled": OrderStatus.SUBMITTED,
    "filled": OrderStatus.FILLED, "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED, "rejected": OrderStatus.REJECTED,
}


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, api_key: str, api_secret: str, paper: bool = True) -> None:
        if not (api_key and api_secret):
            raise RuntimeError("Alpaca broker requires ALPACA_API_KEY and ALPACA_API_SECRET")
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("alpaca-py is not installed; run the install script") from exc
        self.is_paper = paper
        self._client = TradingClient(api_key, api_secret, paper=paper)
        self._fills: list[Fill] = []
        self._meta: dict[str, tuple[str, str]] = {}
        self._symbol_strategies: dict[str, str] = {}

    @property
    def _broker_mode(self) -> str:
        return "paper" if self.is_paper else "live"

    async def connect(self) -> None:
        acct = await asyncio.to_thread(self._client.get_account)
        applog.info("broker.alpaca", f"Connected to Alpaca ({'paper' if self.is_paper else 'LIVE'}) -- "
                                      f"equity ${float(acct.equity):,.2f}")
        await self._bootstrap_durable_fill_baseline()

    async def _bootstrap_durable_fill_baseline(self) -> None:
        """One-time migration baseline; existing installations must not replay old history."""
        from app.db.database import Database
        db = Database.active()
        if db is None or db.has_portfolio_checkpoint(self._broker_mode):
            return
        try:
            from alpaca.trading.requests import GetOrdersRequest
            raw_positions = await asyncio.to_thread(self._client.get_all_positions)
            after = datetime.now(UTC) - timedelta(hours=24)
            orders = await asyncio.to_thread(
                self._client.get_orders, GetOrdersRequest(status="all", after=after, limit=500)
            )
            lots = {}
            for p in raw_positions:
                qty = float(p.qty)
                if str(p.side) == "PositionSide.SHORT" or qty < 0:
                    qty = -abs(qty)
                lots[p.symbol.upper()] = {
                    "qty": qty, "avg_price": float(p.avg_entry_price),
                    "entry_ts": datetime.now(UTC).isoformat(), "strategy": "alpaca",
                    "stop_loss": None, "initial_risk_per_share": None,
                }
            checkpoint = {
                "realized_pnl": 0.0, "day_realized_pnl": 0.0, "session_date": None,
                "session_start_equity": None, "peak_equity": float(acct_equity) if (acct_equity := getattr(await asyncio.to_thread(self._client.get_account), "equity", None)) else 0.0,
                "day_trades_today": 0, "trades": [], "equity_curve": [], "lots": lots,
            }
            watermarks = {
                str(o.id): float(o.filled_qty or 0)
                for o in orders if float(o.filled_qty or 0) > 0
            }
            db.bootstrap_broker_checkpoint(mode=self._broker_mode, checkpoint=checkpoint, fill_watermarks=watermarks)
            applog.info("broker.alpaca", "Initialized durable Alpaca fill baseline from authoritative broker state")
        except Exception as exc:
            applog.warning("broker.alpaca", f"failed to initialize durable fill baseline: {exc}")

    async def account(self) -> AccountSnapshot:
        acct = await asyncio.to_thread(self._client.get_account)
        return AccountSnapshot(equity=float(acct.equity), cash=float(acct.cash), buying_power=float(acct.buying_power))

    async def positions(self) -> list[Position]:
        from app.db.database import Database
        db = Database.active()
        checkpoint = db.get_portfolio_checkpoint(self._broker_mode) if db else None
        if checkpoint is not None:
            market = {}
            try:
                for p in await asyncio.to_thread(self._client.get_all_positions):
                    market[p.symbol.upper()] = float(p.current_price or p.avg_entry_price)
            except Exception:
                pass
            return [
                Position(symbol=symbol, qty=float(lot["qty"]), avg_entry_price=float(lot["avg_price"]),
                         market_price=market.get(symbol, float(lot["avg_price"])), strategy=lot.get("strategy", "alpaca"),
                         opened_at=datetime.fromisoformat(lot["entry_ts"]))
                for symbol, lot in checkpoint.get("lots", {}).items() if abs(float(lot["qty"])) > 1e-9
            ]
        raw = await asyncio.to_thread(self._client.get_all_positions)
        out = []
        for p in raw:
            qty = float(p.qty)
            if str(p.side) == "PositionSide.SHORT" or qty < 0:
                qty = -abs(qty)
            out.append(Position(symbol=p.symbol, qty=qty, avg_entry_price=float(p.avg_entry_price),
                                market_price=float(p.current_price or p.avg_entry_price),
                                strategy=self._symbol_strategies.get(p.symbol, "alpaca")))
        return out

    async def open_orders(self) -> list[Order]:
        from alpaca.trading.requests import GetOrdersRequest
        raw = await asyncio.to_thread(self._client.get_orders, GetOrdersRequest(status="open", limit=200))
        return [self._to_order(o) for o in raw]

    def _to_order(self, o) -> Order:  # noqa: ANN001
        strategy, reason = self._meta.get(str(o.id), ("alpaca", ""))
        return Order(id=str(o.id), client_order_id=str(o.client_order_id or ""), symbol=o.symbol,
                     side=OrderSide.BUY if str(o.side).endswith("BUY") or str(o.side) == "buy" else OrderSide.SELL,
                     qty=float(o.qty or 0), order_type=OrderType.LIMIT if str(o.type).endswith("limit") else OrderType.MARKET,
                     limit_price=float(o.limit_price) if o.limit_price else None,
                     status=_STATUS_MAP.get(str(o.status).split(".")[-1].lower(), OrderStatus.SUBMITTED),
                     filled_qty=float(o.filled_qty or 0), filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
                     strategy=strategy, reason=reason, submitted_at=o.submitted_at or datetime.now(UTC), filled_at=o.filled_at)

    async def submit(self, req: OrderRequest) -> Order:
        from alpaca.trading.enums import OrderSide as ASide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest
        side = ASide.BUY if req.side == OrderSide.BUY else ASide.SELL
        qty = max(1, int(req.qty))
        kwargs: dict = {"symbol": req.symbol.upper(), "qty": qty, "side": side,
                        "time_in_force": TimeInForce.DAY, "client_order_id": req.client_order_id}
        if req.stop_loss is not None and req.take_profit is not None:
            from alpaca.trading.enums import OrderClass
            kwargs["order_class"] = OrderClass.BRACKET
            kwargs["stop_loss"] = StopLossRequest(stop_price=round(req.stop_loss, 2))
            kwargs["take_profit"] = TakeProfitRequest(limit_price=round(req.take_profit, 2))
        request = LimitOrderRequest(limit_price=round(req.limit_price, 2), **kwargs) if req.order_type == OrderType.LIMIT else MarketOrderRequest(**kwargs)
        try:
            raw = await asyncio.to_thread(self._client.submit_order, request)
        except Exception as exc:  # noqa: BLE001
            applog.error("broker.alpaca", f"Order rejected {req.symbol} {req.side} {qty}: {exc}")
            return Order(client_order_id=req.client_order_id, symbol=req.symbol.upper(), side=req.side, qty=qty,
                         order_type=req.order_type, limit_price=req.limit_price, status=OrderStatus.REJECTED,
                         strategy=req.strategy, reason=f"{req.reason} | rejected: {exc}")
        self._meta[str(raw.id)] = (req.strategy, req.reason)
        self._symbol_strategies[req.symbol.upper()] = req.strategy
        order = self._to_order(raw)
        applog.trade("broker.alpaca", f"Submitted {req.side.upper()} {qty} {req.symbol} ({req.strategy})")
        return order

    async def poll_fills(self) -> None:
        """Surface every cumulative execution watermark not durably processed yet."""
        from alpaca.trading.requests import GetOrdersRequest
        from app.db.database import Database
        after = datetime.now(UTC) - timedelta(hours=24)
        try:
            raw = await asyncio.to_thread(self._client.get_orders, GetOrdersRequest(status="all", after=after, limit=500))
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"fill poll failed: {exc}")
            return
        db = Database.active()
        candidates = []
        for o in raw:
            cumulative = float(o.filled_qty or 0)
            if cumulative <= 0 or not o.filled_avg_price:
                continue
            oid = str(o.id)
            processed = db.processed_fill_qty(oid) if db else 0.0
            if cumulative <= processed + 1e-9:
                continue
            candidates.append(o)
        candidates.sort(key=lambda o: (o.filled_at or o.updated_at or o.submitted_at or datetime.now(UTC), str(o.id)))
        queued = {(f.broker_fill_key, f.cumulative_qty) for f in self._fills}
        for o in candidates:
            oid = str(o.id)
            cumulative = float(o.filled_qty or 0)
            if (oid, cumulative) in queued:
                continue
            strategy, reason = self._meta.get(oid, ("alpaca", ""))
            self._fills.append(Fill(order_id=oid, symbol=o.symbol,
                                    side=OrderSide.BUY if "buy" in str(o.side).lower() else OrderSide.SELL,
                                    qty=cumulative, price=float(o.filled_avg_price), strategy=strategy, reason=reason,
                                    ts=o.filled_at or o.updated_at or datetime.now(UTC), broker_fill_key=oid,
                                    cumulative_qty=cumulative, broker_mode=self._broker_mode))

    def drain_fills(self) -> list[Fill]:
        fills, self._fills = self._fills, []
        return fills

    async def cancel(self, order_id: str) -> bool:
        try:
            await asyncio.to_thread(self._client.cancel_order_by_id, order_id)
            return True
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"cancel {order_id} failed: {exc}")
            return False

    async def close_position(self, symbol: str, reason: str = "manual") -> Order | None:
        try:
            raw = await asyncio.to_thread(self._client.close_position, symbol.upper())
            self._meta[str(raw.id)] = ("engine", reason)
            applog.trade("broker.alpaca", f"Closing {symbol} ({reason})")
            return self._to_order(raw)
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"close {symbol} failed: {exc}")
            return None

    async def flatten_all(self, reason: str = "flatten") -> list[Order]:
        try:
            await asyncio.to_thread(self._client.close_all_positions, cancel_orders=True)
            applog.trade("broker.alpaca", f"Flattened all positions ({reason})")
        except Exception as exc:  # noqa: BLE001
            applog.error("broker.alpaca", f"flatten failed: {exc}")
        return []
