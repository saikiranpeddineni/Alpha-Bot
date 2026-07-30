"""Hardened Alpaca production adapter.

This layer keeps the legacy adapter API stable while correcting Alpaca-specific
pagination, lifecycle, borrow, precision, fractional-order, and cancellation
semantics. The broker factory routes production Alpaca instances here.
"""
from __future__ import annotations

from app.brokers import alpaca as legacy
from app.brokers.alpaca_hardening import (
    asset_is_immediately_shortable,
    normalize_order_status,
    normalize_stock_price,
    normalized_qty,
)
from app.brokers.base import BrokerTimeoutError
from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType
from app.core.logging import applog


async def _paginate_orders(client: object, request: object, label: str = "get_orders") -> list:
    """Page Alpaca orders in ascending submission-time order.

    Alpaca's after/until filters are exclusive and based on submitted_at. The API
    exposes no order-id page cursor, so a pathological page containing 500+
    orders with the exact same submitted_at cannot be losslessly advanced. We
    fail visibly if the cursor cannot advance rather than loop or invent a
    filled/updated timestamp cursor.
    """
    from alpaca.common.enums import Sort
    from alpaca.trading.requests import GetOrdersRequest

    page_size = min(getattr(request, "limit", 500) or 500, 500)
    current_after = getattr(request, "after", None)
    before = getattr(request, "until", None)
    status = getattr(request, "status", "all")
    symbols = getattr(request, "symbols", None)
    side = getattr(request, "side", None)
    nested = getattr(request, "nested", None)
    all_orders: list = []
    seen_ids: set[str] = set()

    while True:
        kwargs = {
            "status": status,
            "after": current_after,
            "limit": page_size,
            "direction": Sort.ASC,
        }
        if before is not None:
            kwargs["until"] = before
        if symbols is not None:
            kwargs["symbols"] = symbols
        if side is not None:
            kwargs["side"] = side
        if nested is not None:
            kwargs["nested"] = nested

        page = await legacy._call(
            client.get_orders,
            GetOrdersRequest(**kwargs),
            label=f"{label}/page{len(all_orders) // page_size + 1}",
        )
        if not page:
            break

        new_count = 0
        for order in page:
            oid = str(getattr(order, "id", ""))
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                all_orders.append(order)
                new_count += 1

        if len(page) < page_size:
            break
        if new_count == 0:
            applog.error("broker.alpaca", f"{label}: pagination returned no new order IDs")
            break

        submitted_at = getattr(page[-1], "submitted_at", None)
        if submitted_at is None or submitted_at == current_after:
            applog.error("broker.alpaca", f"{label}: submitted_at pagination cursor did not advance")
            break
        current_after = submitted_at

    return all_orders


# Inherited bootstrap/fill reconciliation resolve this module global at runtime.
legacy._paginate_orders = _paginate_orders


class AlpacaBroker(legacy.AlpacaBroker):
    """Production Alpaca adapter with conservative broker-side safety semantics."""

    def _to_order(self, raw) -> Order:  # noqa: ANN001
        # Conversion is also used during recovery/tests where an adapter can be
        # instantiated without running the network-owning constructor. Strategy
        # attribution is optional metadata, so absence must fall back to Alpaca
        # rather than make otherwise valid broker order state unparseable.
        if not hasattr(self, "_meta"):
            self._meta = {}
        if not hasattr(self, "_symbol_strategies"):
            self._symbol_strategies = {}
        order = super()._to_order(raw)
        order.status = normalize_order_status(getattr(raw, "status", None))
        return order

    async def submit(self, req: OrderRequest) -> Order:
        from alpaca.trading.enums import OrderClass, OrderSide as ASide, TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLimitOrderRequest,
            StopLossRequest,
            StopOrderRequest,
            TakeProfitRequest,
        )

        side = ASide.BUY if req.side == OrderSide.BUY else ASide.SELL
        has_stop = req.stop_loss is not None
        has_target = req.take_profit is not None
        has_advanced_legs = has_stop or has_target
        
        qty = normalized_qty(
            req.qty,
            allow_fractional=req.order_type == OrderType.MARKET and not has_advanced_legs,
        )
        kwargs: dict = {
            "symbol": req.symbol.upper(),
            "qty": qty,
            "side": side,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": req.client_order_id,
        }
        
        if has_advanced_legs:
            if has_stop and has_target:
                kwargs["order_class"] = OrderClass.BRACKET
                kwargs["stop_loss"] = StopLossRequest(stop_price=normalize_stock_price(req.stop_loss))
                kwargs["take_profit"] = TakeProfitRequest(limit_price=normalize_stock_price(req.take_profit))
            elif has_stop:
                kwargs["order_class"] = OrderClass.OTO
                kwargs["stop_loss"] = StopLossRequest(stop_price=normalize_stock_price(req.stop_loss))
            elif has_target:
                kwargs["order_class"] = OrderClass.OTO
                kwargs["take_profit"] = TakeProfitRequest(limit_price=normalize_stock_price(req.take_profit))

        if req.order_type == OrderType.MARKET:
            request = MarketOrderRequest(**kwargs)
        elif req.order_type == OrderType.LIMIT:
            if req.limit_price is None:
                raise ValueError("LIMIT orders require limit_price")
            request = LimitOrderRequest(
                limit_price=normalize_stock_price(req.limit_price), **kwargs
            )
        elif req.order_type == OrderType.STOP:
            if req.stop_price is None:
                raise ValueError("STOP orders require stop_price")
            request = StopOrderRequest(
                stop_price=normalize_stock_price(req.stop_price), **kwargs
            )
        elif req.order_type == OrderType.STOP_LIMIT:
            if req.stop_price is None or req.limit_price is None:
                raise ValueError("STOP_LIMIT orders require stop_price and limit_price")
            request = StopLimitOrderRequest(
                stop_price=normalize_stock_price(req.stop_price),
                limit_price=normalize_stock_price(req.limit_price),
                **kwargs,
            )
        else:
            raise ValueError(f"Unsupported Alpaca order type: {req.order_type}")

        try:
            raw = await legacy._call(
                self._client.submit_order,
                request,
                label=f"submit_order/{req.symbol}",
            )
        except BrokerTimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._is_definitive_rejection(exc):
                msg = (
                    f"Ambiguous submission result for {req.symbol} "
                    f"({req.client_order_id}): {exc}"
                )
                applog.error("broker.alpaca", msg)
                raise BrokerTimeoutError(msg) from exc

            err_msg = str(exc).lower()
            if "not allowed to short" in err_msg or "40310000" in err_msg:
                self._shorting_enabled = False
                self._shortable_cache[req.symbol.upper()] = False
            applog.error(
                "broker.alpaca", f"Order rejected {req.symbol} {req.side} {qty}: {exc}"
            )
            return Order(
                client_order_id=req.client_order_id,
                symbol=req.symbol.upper(),
                side=req.side,
                qty=float(qty),
                order_type=req.order_type,
                limit_price=req.limit_price,
                status=OrderStatus.REJECTED,
                strategy=req.strategy,
                reason=f"{req.reason} | rejected: {exc}",
            )

        self._meta[str(raw.id)] = (req.strategy, req.reason)
        self._symbol_strategies[req.symbol.upper()] = req.strategy
        order = self._to_order(raw)
        order.stop_loss = req.stop_loss
        order.take_profit = req.take_profit
        applog.trade(
            "broker.alpaca",
            f"Submitted {req.side.upper()} {qty} {req.symbol} ({req.strategy})",
        )
        return order

    async def is_shortable(self, symbol: str) -> bool:
        if self._shorting_enabled is False:
            return False
        sym = symbol.upper()
        if sym in self._shortable_cache:
            return self._shortable_cache[sym]

        try:
            acct = await legacy._call(self._client.get_account, label="get_account")
            permission = getattr(acct, "shorting_enabled", None)
            if permission is None:
                applog.warning(
                    "broker.alpaca",
                    "Account response omitted shorting_enabled; failing closed",
                )
                return False
            self._shorting_enabled = bool(permission)
            if not self._shorting_enabled:
                self._shortable_cache[sym] = False
                return False
        except Exception as exc:  # noqa: BLE001
            if self._shorting_enabled is None:
                applog.warning(
                    "broker.alpaca",
                    f"Failed to verify account shorting permission: {exc}",
                )
                return False

        try:
            asset = await legacy._call(
                self._client.get_asset, sym, label=f"get_asset/{sym}"
            )
            shortable = asset_is_immediately_shortable(asset)
            self._shortable_cache[sym] = shortable
            return shortable
        except Exception as exc:  # noqa: BLE001
            applog.warning(
                "broker.alpaca", f"Failed to check shortability for {sym}: {exc}"
            )
            return False

    async def replace_exit_leg(
        self,
        order_id: str,
        *,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order | None:
        """Replace one live bracket/OCO exit leg and verify broker truth.

        Alpaca documents that a successful PATCH does not prove the replacement
        won the race with an execution.  Therefore the returned replacement ID is
        re-fetched and its requested price is verified before callers may treat
        the new protection as authoritative.
        """
        if (limit_price is None) == (stop_price is None):
            raise ValueError("replace_exit_leg requires exactly one price field")
        from alpaca.trading.requests import ReplaceOrderRequest

        kwargs = {}
        if limit_price is not None:
            kwargs["limit_price"] = normalize_stock_price(limit_price)
        else:
            kwargs["stop_price"] = normalize_stock_price(stop_price)
        try:
            raw = await legacy._call(
                self._client.replace_order_by_id,
                order_id,
                ReplaceOrderRequest(**kwargs),
                label=f"replace_order/{order_id}",
            )
            replacement_id = str(getattr(raw, "id", "") or "")
            if not replacement_id:
                applog.warning("broker.alpaca", f"replace {order_id}: broker returned no replacement id")
                return None
            verified = await legacy._call(
                self._client.get_order_by_id,
                replacement_id,
                label=f"verify_replacement/{replacement_id}",
            )
            status = normalize_order_status(getattr(verified, "status", None))
            if status in {OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.FILLED}:
                applog.warning("broker.alpaca", f"replace {order_id}: replacement {replacement_id} is {status}")
                return None
            actual = getattr(verified, "limit_price" if limit_price is not None else "stop_price", None)
            expected = float(limit_price if limit_price is not None else stop_price)
            if actual is None or abs(float(actual) - expected) > 0.011:
                applog.warning(
                    "broker.alpaca",
                    f"replace {order_id}: price verification failed expected={expected:.4f} actual={actual}",
                )
                return None
            return self._to_order(verified)
        except BrokerTimeoutError:
            applog.warning("broker.alpaca", f"replace {order_id} timed out; replacement state is ambiguous")
            return None
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"replace {order_id} failed: {exc}")
            return None

    async def cancel_all(self) -> None:
        """Request cancellation and verify no open orders remain."""
        from alpaca.trading.requests import GetOrdersRequest

        try:
            responses = await legacy._call(
                self._client.cancel_orders, label="cancel_all_orders"
            )
            failures = []
            for response in responses or []:
                status = getattr(response, "status", None)
                if status is not None and not (200 <= int(status) < 300):
                    failures.append(str(getattr(response, "id", "unknown")))
            if failures:
                applog.warning(
                    "broker.alpaca",
                    f"cancel_all: broker reported cancellation failures: {failures}",
                )

            remaining = await legacy._call(
                self._client.get_orders,
                GetOrdersRequest(status="open", limit=500),
                label="get_orders/cancel_all_verify",
            )
            if remaining:
                ids = [str(getattr(o, "id", "unknown")) for o in remaining]
                applog.error(
                    "broker.alpaca",
                    f"cancel_all verification failed; open orders remain: {ids}",
                )
                return
            applog.info("broker.alpaca", "cancel_all: verified no open orders remain")
        except BrokerTimeoutError:
            applog.error("broker.alpaca", "cancel_all timed out; state is ambiguous")
        except Exception as exc:  # noqa: BLE001
            applog.warning(
                "broker.alpaca", f"cancel_all failed or could not be verified: {exc}"
            )
