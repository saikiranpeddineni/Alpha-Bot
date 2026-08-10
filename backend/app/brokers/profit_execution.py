"""Execution wrappers that preserve PIS ownership without changing broker APIs."""

from __future__ import annotations

import functools

class ProtectionRecreationError(Exception):
    """Raised when broker protection cannot be safely recreated after advanced-order cancellation."""


def install_profit_execution_hooks(broker) -> None:
    """Install idempotent broker hooks for target metadata and partial exits."""
    if getattr(broker, "_profit_execution_hooks_installed", False):
        return

    original_submit = broker.submit

    @functools.wraps(original_submit)
    async def submit_without_discretionary_target(req):
        # Strategy targets remain available to PIS through the local Order record,
        # but are never sent as native broker take-profit legs. This preserves the
        # existing PIS single-owner exit architecture.
        target = getattr(req, "take_profit", None)
        if target is None:
            return await original_submit(req)

        safe_req = req.model_copy(update={"take_profit": None})
        order = await original_submit(safe_req)
        if order is not None:
            order.take_profit = target
        return order

    broker.submit = submit_without_discretionary_target

    # The production hardened Alpaca adapter gets a protection-preserving partial
    # close implementation. Other brokers keep their existing close semantics.
    if broker.__class__.__name__ == "AlpacaBroker" and hasattr(broker, "_client"):
        original_close = broker.close_position

        @functools.wraps(original_close)
        async def close_position_profit_safe(symbol: str, reason: str = "manual", qty: float | None = None):
            if qty is None or qty <= 0:
                return await original_close(symbol, reason=reason, qty=qty)

            try:
                from alpaca.trading.requests import ClosePositionRequest, ReplaceOrderRequest
                from app.brokers import alpaca as legacy
                from app.brokers.alpaca_hardening import normalize_stock_price, normalized_qty
                from app.brokers.base import BrokerTimeoutError
                from app.core.domain import OrderSide, OrderStatus, OrderType

                positions = await broker.positions()
                position = next((p for p in positions if p.symbol.upper() == symbol.upper()), None)
                if position is None:
                    return await original_close(symbol, reason=reason, qty=qty)

                current_qty = abs(float(position.qty))
                close_qty = min(float(qty), current_qty)
                remaining_qty = current_qty - close_qty

                # Full exits can safely use the existing broker helper because no
                # position remains that needs protection.
                if remaining_qty <= 0:
                    return await original_close(symbol, reason=reason, qty=None)

                protective_side = OrderSide.SELL if position.qty > 0 else OrderSide.BUY
                open_orders = await broker.open_orders()
                stop_candidates = [
                    o for o in open_orders
                    if o.symbol.upper() == symbol.upper()
                    and o.side == protective_side
                    and o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
                    and o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
                    and o.stop_price is not None
                ]
                stop_candidates.sort(key=lambda o: (0 if o.parent_id else 1, o.submitted_at or 0))

                # Never call the legacy partial-close helper if the remaining
                # position cannot be proven protected: that helper cancels open
                # orders before closing and would recreate the protection gap.
                if not stop_candidates:
                    raise RuntimeError(
                        f"Refusing partial close for {symbol}: no authoritative protective stop found for remaining qty {remaining_qty:g}"
                    )

                stop = stop_candidates[0]
                replace_kwargs = {"qty": normalized_qty(remaining_qty, allow_fractional=False)}
                replace_kwargs["stop_price"] = normalize_stock_price(stop.stop_price)
                if stop.order_type == OrderType.STOP_LIMIT and stop.limit_price is not None:
                    replace_kwargs["limit_price"] = normalize_stock_price(stop.limit_price)

                try:
                    raw = await legacy._call(
                        broker._client.replace_order_by_id,
                        stop.id,
                        ReplaceOrderRequest(**replace_kwargs),
                        label=f"resize_protection/{stop.id}",
                    )
                except Exception as exc:
                    # Alpaca rejects qty changes on advanced orders (OTO/Bracket).
                    if "qty cannot be changed" in str(exc).lower():
                        from alpaca.trading.requests import StopOrderRequest
                        from alpaca.trading.enums import TimeInForce
                        
                        await legacy._call(broker._client.cancel_order_by_id, stop.id, label=f"cancel_advanced_stop/{stop.id}")
                        
                        req = StopOrderRequest(
                            symbol=symbol.upper(),
                            qty=replace_kwargs["qty"],
                            side=stop.side,
                            time_in_force=TimeInForce.DAY,
                            stop_price=replace_kwargs["stop_price"],
                        )
                        try:
                            raw = await legacy._call(broker._client.submit_order, req, label=f"recreate_protection/{symbol}")
                        except Exception as recreate_exc:
                            raise ProtectionRecreationError(f"CRITICAL: Failed to recreate protection for {symbol}: {recreate_exc}")
                    else:
                        raise
                replacement_id = str(getattr(raw, "id", "") or "")
                if not replacement_id:
                    raise RuntimeError(f"broker returned no replacement id for protection {stop.id}")

                verified = await legacy._call(
                    broker._client.get_order_by_id,
                    replacement_id,
                    label=f"verify_resized_protection/{replacement_id}",
                )
                verified_qty = float(getattr(verified, "qty", 0.0) or 0.0)
                if abs(verified_qty - remaining_qty) > max(1e-6, remaining_qty * 1e-6):
                    raise RuntimeError(
                        f"protection quantity verification failed expected={remaining_qty} actual={verified_qty}"
                    )

                # Protection quantity is now reduced to the post-scale-out exposure,
                # so the close can execute without canceling the remaining stop.
                raw_close = await legacy._call(
                    broker._client.close_position,
                    symbol.upper(),
                    ClosePositionRequest(
                        qty=str(int(close_qty)) if float(close_qty).is_integer() else str(round(close_qty, 9))
                    ),
                    label=f"close_position_partial_safe/{symbol}",
                )
                broker._meta[str(raw_close.id)] = ("engine", reason)
                return broker._to_order(raw_close)
            except BrokerTimeoutError:
                raise
            except Exception:
                # Fail closed: do not fall back to the legacy helper after a
                # protection replacement failure because that helper cancels the
                # remaining stop and recreates the exact profit-leak window.
                raise

        broker.close_position = close_position_profit_safe

    broker._profit_execution_hooks_installed = True
