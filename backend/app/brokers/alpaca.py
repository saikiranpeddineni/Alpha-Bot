"""Alpaca broker adapter (alpaca-py SDK).

Maps the platform's order model onto Alpaca's REST API. Entries with a stop
and target become native bracket orders so protective exits live server-side
at the broker even if this machine goes offline. Fills are detected by
polling order state each engine tick and surfaced through drain_fills().

All Alpaca SDK calls are wrapped with asyncio.wait_for(asyncio.to_thread(...))
using a bounded wall-clock budget (ALPACA_API_TIMEOUT_SECONDS, default 10 s).
A timeout raises BrokerTimeoutError so callers treat it as an ambiguous state
rather than success or clean failure, preserving engine-tick liveness.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from app.brokers.base import Broker, BrokerTimeoutError, Fill
from app.core.domain import (
    AccountSnapshot,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from app.core.logging import applog

# Wall-clock budget for every Alpaca HTTP call. Tune via env var if needed.
_API_TIMEOUT: float = float(os.environ.get("ALPACA_API_TIMEOUT_SECONDS", "10"))

_STATUS_MAP = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.SUBMITTED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


async def _call(fn, *args, timeout: float = _API_TIMEOUT, label: str = ""):
    """Run a synchronous Alpaca SDK call in a thread with a bounded timeout.

    Raises BrokerTimeoutError (not asyncio.TimeoutError) so callers can
    distinguish this from other failures without catching the base exception.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        msg = f"Alpaca API call timed out after {timeout:.0f}s"
        if label:
            msg += f" [{label}]"
        applog.error("broker.alpaca", msg)
        raise BrokerTimeoutError(msg) from exc


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
        self._emitted_fill_ids: set[str] = set()
        self._processed_fill_qty: dict[str, float] = {}
        self._meta: dict[str, tuple[str, str]] = {}  # order id -> (strategy, reason)
        self._symbol_strategies: dict[str, str] = {}
        self.account_id: str | None = None

    @property
    def _broker_mode(self) -> str:
        return "paper" if self.is_paper else "live"

    async def connect(self) -> None:
        acct = await _call(self._client.get_account, label="get_account")
        self.account_id = str(acct.account_number)
        applog.info(
            "broker.alpaca",
            f"Connected to Alpaca ({'paper' if self.is_paper else 'LIVE'}) account {self.account_id} -- equity ${float(acct.equity):,.2f}",
        )
        await self._bootstrap_durable_fill_baseline()

    async def _bootstrap_durable_fill_baseline(self) -> None:
        """One-time migration baseline; existing installations must not replay old history."""
        from app.db.database import Database
        db = Database.active()
        if db is None or db.has_portfolio_checkpoint(self._broker_mode):
            return
        try:
            from alpaca.trading.requests import GetOrdersRequest
            raw_positions = await _call(self._client.get_all_positions, label="get_all_positions")
            after = datetime.now(UTC) - timedelta(hours=24)
            orders = await _call(
                self._client.get_orders,
                GetOrdersRequest(status="all", after=after, limit=500),
                label="get_orders/bootstrap",
            )
            lots = {}
            for p in raw_positions:
                qty = float(p.qty)
                if str(p.side) == "PositionSide.SHORT" or qty < 0:
                    qty = -abs(qty)
                lots[p.symbol.upper()] = {
                    "qty": qty,
                    "avg_price": float(p.avg_entry_price),
                    "stop_loss": None,
                    "initial_risk_per_share": None,
                }
            checkpoint = {
                "realized_pnl": 0.0,
                "day_realized_pnl": 0.0,
                "session_date": None,
                "session_start_equity": None,
                "peak_equity": float(acct.equity) if (acct := await _call(self._client.get_account, label="get_account")) else 0.0,
                "day_trades_today": 0,
                "trades": [],
                "equity_curve": [],
                "lots": lots,
            }
            watermarks = {
                str(o.id): float(o.filled_qty or 0)
                for o in orders if float(o.filled_qty or 0) > 0
            }
            db.bootstrap_broker_checkpoint(mode=self._broker_mode, account_id=self.account_id, checkpoint=checkpoint, fill_watermarks=watermarks)
            applog.info("broker.alpaca", "Initialized durable Alpaca fill baseline from authoritative broker state")
        except Exception as exc:
            applog.warning("broker.alpaca", f"failed to initialize durable fill baseline: {exc}")

    async def account(self) -> AccountSnapshot:
        acct = await _call(self._client.get_account, label="get_account")
        return AccountSnapshot(
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power),
        )

    async def positions(self) -> list[Position]:
        raw = await _call(self._client.get_all_positions, label="get_all_positions")
        out = []
        for p in raw:
            qty = float(p.qty)
            if str(p.side) == "PositionSide.SHORT" or qty < 0:
                qty = -abs(qty)
            out.append(
                Position(
                    symbol=p.symbol,
                    qty=qty,
                    avg_entry_price=float(p.avg_entry_price),
                    market_price=float(p.current_price or p.avg_entry_price),
                    strategy=self._symbol_strategies.get(p.symbol, "alpaca"),
                )
            )
        return out

    async def open_orders(self) -> list[Order]:
        from alpaca.trading.requests import GetOrdersRequest

        raw = await _call(
            self._client.get_orders,
            GetOrdersRequest(status="open", limit=200),
            label="get_orders/open",
        )
        return [self._to_order(o) for o in raw]

    def _to_order(self, o) -> Order:  # noqa: ANN001
        strategy, reason = self._meta.get(str(o.id), ("alpaca", ""))
        return Order(
            id=str(o.id),
            client_order_id=str(o.client_order_id or ""),
            symbol=o.symbol,
            side=OrderSide.BUY if str(o.side).endswith("BUY") or str(o.side) == "buy" else OrderSide.SELL,
            qty=float(o.qty or 0),
            order_type=OrderType.LIMIT if str(o.type).endswith("limit") else OrderType.MARKET,
            limit_price=float(o.limit_price) if o.limit_price else None,
            status=_STATUS_MAP.get(str(o.status).split(".")[-1].lower(), OrderStatus.SUBMITTED),
            filled_qty=float(o.filled_qty or 0),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
            strategy=strategy,
            reason=reason,
            submitted_at=o.submitted_at or datetime.now(UTC),
            filled_at=o.filled_at,
        )

    async def submit(self, req: OrderRequest) -> Order:
        from alpaca.trading.enums import OrderSide as ASide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        side = ASide.BUY if req.side == OrderSide.BUY else ASide.SELL
        qty = max(1, int(req.qty))  # brackets require whole shares
        kwargs: dict = {
            "symbol": req.symbol.upper(),
            "qty": qty,
            "side": side,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": req.client_order_id,
        }
        if req.stop_loss is not None and req.take_profit is not None:
            from alpaca.trading.enums import OrderClass

            kwargs["order_class"] = OrderClass.BRACKET
            kwargs["stop_loss"] = StopLossRequest(stop_price=round(req.stop_loss, 2))
            kwargs["take_profit"] = TakeProfitRequest(limit_price=round(req.take_profit, 2))

        if req.order_type == OrderType.LIMIT:
            request = LimitOrderRequest(limit_price=round(req.limit_price, 2), **kwargs)
        else:
            request = MarketOrderRequest(**kwargs)

        try:
            raw = await _call(self._client.submit_order, request, label=f"submit_order/{req.symbol}")
        except BrokerTimeoutError:
            # Propagate: caller must treat as ambiguous order state, not rejected.
            raise
        except Exception as exc:  # noqa: BLE001
            if self._is_definitive_rejection(exc):
                applog.error("broker.alpaca", f"Order rejected {req.symbol} {req.side} {qty}: {exc}")
                return Order(
                    client_order_id=req.client_order_id,
                    symbol=req.symbol.upper(),
                    side=req.side,
                    qty=qty,
                    order_type=req.order_type,
                    limit_price=req.limit_price,
                    status=OrderStatus.REJECTED,
                    strategy=req.strategy,
                    reason=f"{req.reason} | rejected: {exc}",
                )
            else:
                msg = f"Ambiguous submission result for {req.symbol} ({req.client_order_id}): {exc}"
                applog.error("broker.alpaca", msg)
                raise BrokerTimeoutError(msg) from exc
        self._meta[str(raw.id)] = (req.strategy, req.reason)
        self._symbol_strategies[req.symbol.upper()] = req.strategy
        order = self._to_order(raw)
        applog.trade("broker.alpaca", f"Submitted {req.side.upper()} {qty} {req.symbol} ({req.strategy})")
        return order

    def _is_definitive_rejection(self, exc: Exception) -> bool:
        import asyncio
        try:
            import requests
            if isinstance(exc, (requests.exceptions.RequestException, ConnectionError, TimeoutError, asyncio.TimeoutError)):
                return False
        except ImportError:
            if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
                return False

        try:
            from alpaca.common.exceptions import APIError
            if isinstance(exc, APIError):
                status_code = getattr(exc, "status_code", None)
                if status_code is not None:
                    # 4xx are client/business rejections, 5xx are server/transport issues
                    return 400 <= status_code < 500
                return True
        except ImportError:
            pass

        # If it's a timeout/connection description, treat as transport failure
        err_str = str(exc).lower()
        if "timeout" in err_str or "connect" in err_str or "connection" in err_str:
            return False

        return True

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        try:
            raw = await _call(
                self._client.get_order_by_client_id,
                client_order_id,
                label=f"get_order_by_client_id/{client_order_id}"
            )
            return self._to_order(raw)
        except BrokerTimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            from alpaca.common.exceptions import APIError
            if isinstance(exc, APIError) and getattr(exc, "status_code", None) == 404:
                return None
            err_str = str(exc).lower()
            if "not found" in err_str or "404" in err_str:
                return None
            raise BrokerTimeoutError(f"Ambiguous query result for client ID {client_order_id}: {exc}") from exc

    async def poll_fills(self) -> None:
        """Surface every cumulative execution watermark not durably processed yet."""
        from alpaca.trading.requests import GetOrdersRequest
        from app.db.database import Database

        after = datetime.now(UTC) - timedelta(hours=24)
        try:
            raw = await _call(
                self._client.get_orders,
                GetOrdersRequest(status="all", after=after, limit=500),
                label="get_orders/poll_fills",
            )
        except BrokerTimeoutError:
            applog.warning("broker.alpaca", "poll_fills skipped: API call timed out")
            return
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
            if db:
                processed = db.processed_fill_qty(mode=self._broker_mode, account_id=self.account_id, fill_key=oid)
            else:
                processed = self._processed_fill_qty.get(oid, 0.0)
            if cumulative <= processed + 1e-9:
                continue
            candidates.append(o)

        candidates.sort(key=lambda o: (o.filled_at or o.updated_at or o.submitted_at or datetime.now(UTC), str(o.id)))
        queued = {(f.broker_fill_key, f.cumulative_qty) for f in self._fills}
        local_watermarks = {}
        for o in candidates:
            oid = str(o.id)
            cumulative = float(o.filled_qty or 0)
            if (oid, cumulative) in queued:
                continue
            if oid in local_watermarks:
                processed = local_watermarks[oid]
            elif db:
                processed = db.processed_fill_qty(mode=self._broker_mode, account_id=self.account_id, fill_key=oid)
            else:
                processed = self._processed_fill_qty.get(oid, 0.0)
            delta = cumulative - processed
            if delta <= 1e-9:
                continue
            strategy, reason = self._meta.get(oid, ("alpaca", ""))
            self._fills.append(
                Fill(
                    order_id=oid,
                    symbol=o.symbol,
                    side=OrderSide.BUY if "buy" in str(o.side).lower() else OrderSide.SELL,
                    qty=delta,
                    price=float(o.filled_avg_price),
                    strategy=strategy,
                    reason=reason,
                    ts=o.filled_at or o.updated_at or datetime.now(UTC),
                    broker_fill_key=oid,
                    cumulative_qty=cumulative,
                    broker_mode=self._broker_mode,
                    broker_account_id=self.account_id,
                )
            )
            self._processed_fill_qty[oid] = cumulative
            local_watermarks[oid] = cumulative

    def drain_fills(self) -> list[Fill]:
        fills, self._fills = self._fills, []
        return fills

    async def cancel(self, order_id: str) -> bool:
        try:
            await _call(self._client.cancel_order_by_id, order_id, label=f"cancel/{order_id}")
            return True
        except BrokerTimeoutError:
            applog.warning("broker.alpaca", f"cancel {order_id} timed out; state is ambiguous")
            return False
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"cancel {order_id} failed: {exc}")
            return False

    async def close_position(self, symbol: str, reason: str = "manual") -> Order | None:
        try:
            raw = await _call(self._client.close_position, symbol.upper(), label=f"close_position/{symbol}")
            self._meta[str(raw.id)] = ("engine", reason)
            applog.trade("broker.alpaca", f"Closing {symbol} ({reason})")
            return self._to_order(raw)
        except BrokerTimeoutError:
            applog.warning("broker.alpaca", f"close_position {symbol} timed out; state is ambiguous")
            return None
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"close {symbol} failed: {exc}")
            return None

    async def flatten_all(self, reason: str = "flatten") -> list[Order]:
        try:
            await _call(self._client.close_all_positions, cancel_orders=True, label="close_all_positions")
            applog.trade("broker.alpaca", f"Flattened all positions ({reason})")
        except BrokerTimeoutError:
            applog.error("broker.alpaca", "flatten_all timed out; manual intervention may be required")
        except Exception as exc:  # noqa: BLE001
            applog.error("broker.alpaca", f"flatten failed: {exc}")
        return []
