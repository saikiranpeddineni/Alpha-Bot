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
from app.core.applog import applog
from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType


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
        from alpaca.trading.enums import OrderClass, TimeInForce
        from alpaca.trading.enums import OrderSide as ASide
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
        # =============================================================================
        # [MODIFIED 2026-10-23]: Auto-Heal 42210000 Stop Price Geometry Rejections on Submit
        # ORIGINAL INTENT: Unconditionally marked order as REJECTED when submit_order threw an exception.
        # REASON FOR CHANGE: Live market price drift frequently pushes price past requested stop price
        # before the HTTP request arrives, triggering Alpaca 42210000 ("stop price must be greater/less than current price").
        # By parsing the live market_price from Alpaca's error JSON, clamping stop_price (+0.05 for buy stops, -0.05 for sell stops),
        # and retrying immediately, the protective stop order is safely and automatically healed.
        # SAFE ROLLBACK: Previous exception handler preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # except Exception as exc:  # noqa: BLE001
        #     if not self._is_definitive_rejection(exc):
        #         msg = (
        #             f"Ambiguous submission result for {req.symbol} "
        #             f"({req.client_order_id}): {exc}"
        #         )
        #         applog.error("broker.alpaca", msg)
        #         raise BrokerTimeoutError(msg) from exc
        #     err_msg = str(exc).lower()
        #     if "not allowed to short" in err_msg or "40310000" in err_msg:
        #         self._shorting_enabled = False
        #         self._shortable_cache[req.symbol.upper()] = False
        #     applog.error(
        #         "broker.alpaca", f"Order rejected {req.symbol} {req.side} {qty}: {exc}"
        #     )
        #     return Order(
        #         client_order_id=req.client_order_id,
        #         symbol=req.symbol.upper(),
        #         side=req.side,
        #         qty=float(qty),
        #         order_type=req.order_type,
        #         limit_price=req.limit_price,
        #         status=OrderStatus.REJECTED,
        #         strategy=req.strategy,
        #         reason=f"{req.reason} | rejected: {exc}",
        #     )
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        except Exception as exc:  # noqa: BLE001
            err_s = str(exc)
            if "42210000" in err_s and ("greater than current price" in err_s or "less than current price" in err_s):
                import re
                m_px = re.search(r'"market_price":\s*"([0-9\.]+)"', err_s)
                if m_px:
                    live_mkt = float(m_px.group(1))
                    is_buy = req.side == OrderSide.BUY
                    healed_stop = round(live_mkt + 0.05 if is_buy else live_mkt - 0.05, 2)
                    if hasattr(request, "stop_price"):
                        request.stop_price = normalize_stock_price(healed_stop)
                    try:
                        raw = await legacy._call(
                            self._client.submit_order,
                            request,
                            label=f"submit_order/{req.symbol}_healed",
                        )
                        applog.info(
                            "broker.alpaca",
                            f"Auto-healed submitted stop price for {req.symbol} to {healed_stop:.2f} (market: {live_mkt:.2f})",
                        )
                        self._meta[str(raw.id)] = (req.strategy, req.reason)
                        self._symbol_strategies[req.symbol.upper()] = req.strategy
                        order = self._to_order(raw)
                        order.stop_loss = healed_stop
                        order.take_profit = req.take_profit
                        applog.trade(
                            "broker.alpaca",
                            f"Submitted {req.side.upper()} {qty} {req.symbol} ({req.strategy}) [healed stop @ {healed_stop:.2f}]",
                        )
                        return order
                    except Exception as heal_exc:
                        applog.warning("broker.alpaca", f"Auto-heal retry failed for {req.symbol}: {heal_exc}")

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
        # =============================================================================

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
        # =============================================================================
        # [MODIFIED 2026-10-23]: Auto-Heal 42210000 Stop Price Geometry on Replacement
        # ORIGINAL INTENT: Caught all exceptions, logged warning, and returned None.
        # REASON FOR CHANGE: Live market price drift pushes market past stop price, triggering
        # Alpaca 42210000. Parsing market_price and clamping (+0.05 / -0.05) auto-heals replacement.
        # SAFE ROLLBACK: Previous lines preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # except Exception as exc:  # noqa: BLE001
        #     applog.warning("broker.alpaca", f"replace {order_id} failed: {exc}")
        #     return None
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        except Exception as exc:  # noqa: BLE001
            err_s = str(exc)
            if "42210000" in err_s and ("greater than current price" in err_s or "less than current price" in err_s) and stop_price is not None:
                import re
                m_px = re.search(r'"market_price":\s*"([0-9\.]+)"', err_s)
                if m_px:
                    live_mkt = float(m_px.group(1))
                    is_buy_stop = "greater than current price" in err_s
                    healed_stop = round(live_mkt + 0.05 if is_buy_stop else live_mkt - 0.05, 2)
                    try:
                        kwargs2 = dict(kwargs)
                        kwargs2["stop_price"] = normalize_stock_price(healed_stop)
                        raw2 = await legacy._call(
                            self._client.replace_order_by_id,
                            order_id,
                            ReplaceOrderRequest(**kwargs2),
                            label=f"replace_order/{order_id}_healed",
                        )
                        replacement_id = str(getattr(raw2, "id", "") or "")
                        if replacement_id:
                            verified2 = await legacy._call(
                                self._client.get_order_by_id,
                                replacement_id,
                                label=f"verify_replacement/{replacement_id}",
                            )
                            applog.info(
                                "broker.alpaca",
                                f"Auto-healed stop price replacement for order {order_id} to {healed_stop:.2f} (market: {live_mkt:.2f})",
                            )
                            return self._to_order(verified2)
                    except Exception as heal_exc2:
                        applog.warning("broker.alpaca", f"Auto-heal replace failed for {order_id}: {heal_exc2}")

            applog.warning("broker.alpaca", f"replace {order_id} failed: {exc}")
            return None
        # =============================================================================

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

    # =============================================================================
    # [MODIFIED 2026-10-23]: Implement update_stop_loss and resize_protective_orders on AlpacaBroker
    # ORIGINAL INTENT: AlpacaBroker relied on engine-level synthetic stops or replace_exit_leg only.
    # REASON FOR CHANGE: Complete Two-Step Peak-R Ladder requires high-level stop ratcheting
    # and odd-lot protective order resizing with strict monotonic protection and auto-healing.
    # SAFE ROLLBACK: Remove these methods; falls back to Broker base no-ops.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    async def update_stop_loss(self, symbol: str, stop_price: float) -> bool:
        """Ratchet the protective stop of an open position to a better price with monotonic guard and 422 healing."""
        import asyncio
        from alpaca.trading.enums import OrderSide as ASide, QueryOrderStatus, TimeInForce
        from alpaca.trading.requests import GetOrdersRequest, ReplaceOrderRequest, StopOrderRequest

        sym = symbol.upper()
        try:
            raw = await legacy._call(
                self._client.get_orders,
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym]),
                label=f"get_orders/{sym}",
            )
            own_entries = self._own_working_entries(raw) if hasattr(self, "_own_working_entries") else set()
            stop_orders = [
                o for o in raw
                if str(getattr(o, "id", "")) not in own_entries and "stop" in str(getattr(o, "type", "")).lower()
            ]

            pos = None
            try:
                pos = await legacy._call(self._client.get_open_position, sym, label=f"get_open_position/{sym}")
                qty = int(float(stop_orders[0].qty)) if stop_orders else int(abs(float(pos.qty)))
                is_long = float(pos.qty) > 0
                exit_side = ASide.SELL if is_long else ASide.BUY
                current_price = float(pos.current_price) if getattr(pos, "current_price", None) else None
            except Exception:
                qty = int(float(stop_orders[0].qty)) if stop_orders else 0
                is_long = True
                exit_side = ASide.SELL
                current_price = None

            if current_price is not None and current_price > 0:
                if is_long and stop_price >= current_price:
                    applog.warning(
                        "broker.alpaca",
                        f"Refusing to update stop for long {sym}: stop {stop_price:.2f} >= current price {current_price:.2f}",
                    )
                    return False
                if not is_long and stop_price <= current_price:
                    applog.warning(
                        "broker.alpaca",
                        f"Refusing to update stop for short {sym}: stop {stop_price:.2f} <= current price {current_price:.2f}",
                    )
                    return False

            # Strict Monotonic Ratchet Guard: never downgrade stops
            for o in stop_orders:
                cur_sp = float(o.stop_price) if getattr(o, "stop_price", None) else None
                if cur_sp is not None:
                    if abs(cur_sp - stop_price) < 0.01:
                        return True
                    if is_long and stop_price < cur_sp - 0.01:
                        applog.warning(
                            "broker.alpaca",
                            f"{sym}: Refusing to downgrade stop for long: requested {stop_price:.2f} < existing {cur_sp:.2f}",
                        )
                        return True
                    elif not is_long and stop_price > cur_sp + 0.01:
                        applog.warning(
                            "broker.alpaca",
                            f"{sym}: Refusing to downgrade stop for short: requested {stop_price:.2f} > existing {cur_sp:.2f}",
                        )
                        return True

            # Replace existing stop order
            for o in stop_orders:
                try:
                    req_replace = ReplaceOrderRequest(stop_price=normalize_stock_price(stop_price))
                    res = await legacy._call(self._client.replace_order_by_id, str(o.id), req_replace, label=f"replace_order/{o.id}")
                    if res and hasattr(res, "id"):
                        self._meta[str(res.id)] = ("alpaca", "stop_loss")
                    return True
                except Exception as e:
                    err_s = str(e)
                    if "42210000" in err_s and ("greater than current price" in err_s or "less than current price" in err_s):
                        import re
                        m_px = re.search(r'"market_price":\s*"([0-9\.]+)"', err_s)
                        if m_px:
                            live_mkt = float(m_px.group(1))
                            healed_stop = round(live_mkt + 0.05 if not is_long else live_mkt - 0.05, 2)
                            try:
                                req_healed = ReplaceOrderRequest(stop_price=normalize_stock_price(healed_stop))
                                res2 = await legacy._call(self._client.replace_order_by_id, str(o.id), req_healed, label=f"replace_order/{o.id}_healed")
                                if res2 and hasattr(res2, "id"):
                                    self._meta[str(res2.id)] = ("alpaca", "stop_loss")
                                applog.info(
                                    "broker.alpaca",
                                    f"Auto-healed stop price geometry for {sym} to {healed_stop:.2f} (market: {live_mkt:.2f})",
                                )
                                return True
                            except Exception:
                                pass
                    applog.warning("broker.alpaca", f"replace_order_by_id for {sym} ({o.id}) failed: {e}")
                    break

            # If no open stop order exists, submit a clean protective stop
            if not stop_orders and qty > 0:
                new_req = StopOrderRequest(
                    symbol=sym,
                    qty=qty,
                    side=exit_side,
                    time_in_force=TimeInForce.DAY,
                    stop_price=normalize_stock_price(stop_price),
                )
                try:
                    new_order = await legacy._call(self._client.submit_order, new_req, label=f"submit_clean_stop/{sym}")
                except Exception as sub_err:
                    sub_s = str(sub_err)
                    if "42210000" in sub_s and ("greater than current price" in sub_s or "less than current price" in sub_s):
                        import re
                        m_px = re.search(r'"market_price":\s*"([0-9\.]+)"', sub_s)
                        if m_px:
                            live_mkt = float(m_px.group(1))
                            healed_stop = round(live_mkt + 0.05 if not is_long else live_mkt - 0.05, 2)
                            new_req.stop_price = normalize_stock_price(healed_stop)
                            new_order = await legacy._call(self._client.submit_order, new_req, label=f"submit_clean_stop/{sym}_healed")
                        else:
                            raise
                    else:
                        raise
                self._meta[str(new_order.id)] = ("alpaca", "stop_loss")
                applog.info("broker.alpaca", f"Set clean protective stop for {sym}: {qty} shares @ {new_req.stop_price:.2f}")
                return True

            return False
        except Exception as exc:
            applog.error("broker.alpaca", f"update_stop_loss {sym} -> {stop_price:.2f} failed: {exc}")
            return False

    async def resize_protective_orders(self, symbol: str, target_qty: float) -> None:
        """Resize active protective orders after a partial position scale-out."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest, ReplaceOrderRequest

        sym = symbol.upper()
        target_qty = float(int(target_qty))
        if target_qty <= 0:
            return
        try:
            raw = await legacy._call(
                self._client.get_orders,
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym]),
                label=f"get_orders/{sym}",
            )
            own_entries = self._own_working_entries(raw) if hasattr(self, "_own_working_entries") else set()
            stop_orders = [
                o for o in raw
                if str(getattr(o, "id", "")) not in own_entries and "stop" in str(getattr(o, "type", "")).lower()
            ]
            for o in stop_orders:
                cur_qty = float(getattr(o, "qty", 0.0) or 0.0)
                if abs(cur_qty - target_qty) > 0.001:
                    try:
                        req_replace = ReplaceOrderRequest(qty=int(target_qty))
                        res = await legacy._call(self._client.replace_order_by_id, str(o.id), req_replace, label=f"resize_order/{o.id}")
                        if res and hasattr(res, "id"):
                            self._meta[str(res.id)] = ("alpaca", "stop_loss")
                        applog.info("broker.alpaca", f"Resized protective stop for {sym} to {target_qty:.0f} shares")
                    except Exception as resize_exc:
                        applog.warning("broker.alpaca", f"Failed to resize stop order {o.id} for {sym}: {resize_exc}")
        except Exception as exc:
            applog.error("broker.alpaca", f"Failed to resize protective orders for {sym}: {exc}")
    # =============================================================================
