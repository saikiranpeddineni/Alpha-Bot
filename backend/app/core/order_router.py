"""TradingEngineOrderRouterMixin.

Service boundary extracting order routing, guarded close execution,
and manual order operations from TradingEngine while maintaining 100% backward compatibility.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
from typing import Any
from zoneinfo import ZoneInfo

from app.core.applog import applog
from app.core.domain import (
    EngineState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    utcnow,
)


class TradingEngineOrderRouterMixin:
    """Order routing, manual order entry, and guarded close execution mixin for TradingEngine."""

    async def _close_position_guarded(
        self,
        symbol: str,
        reason: str,
        qty: float | None = None,
        strategy: str | None = None,
    ) -> bool:
        """Close a position once and report whether the broker accepted the close.

        ``True`` means a close order with a usable identifier was accepted by
        the broker (it may still be waiting to fill).  ``False`` means the
        broker returned no order or explicitly rejected it.  A broker
        exception is re-raised after releasing the latch because that is an
        ambiguous result.  A duplicate call is treated as ``True`` because
        the existing in-flight close already owns the symbol's close intent;
        this preserves the idempotent close latch without reverting PIS state
        for a request that was never submitted.
        """
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        if not hasattr(self, "_pending_close_order_ids"):
            self._pending_close_order_ids = {}
        # fix(partial-close-wedge): track requested close size and submit time so
        # _drain_fills can release the guard from fill truth alone and stale
        # latches can never wedge PIS exits indefinitely (2026-08-24: after the
        # CRWV/V/AMZN partial scale-outs every subsequent PROTECTION_BREACH
        # exit was silently swallowed for 30-80 minutes).
        if not hasattr(self, "_pending_close_qtys"):
            self._pending_close_qtys = {}
        if not hasattr(self, "_pending_close_submitted_at"):
            self._pending_close_submitted_at = {}

        # fix(#45): also guard against concurrent per-tick flatten submissions
        if symbol in self._pending_close_symbols or symbol in self._flattening_symbols:
            applog.debug("engine", f"{symbol}: skipping duplicate close — close already in-flight (#45)")
            return True

        self._flattening_symbols.add(symbol)
        self._pending_close_symbols.add(symbol)
        try:
            # Support brokers whose close_position predates the qty parameter.
            # Signature inspection (not try/except TypeError) so a TypeError
            # raised INSIDE broker logic can never silently convert a targeted
            # partial close into a full-position close.
            close_parameters = inspect.signature(self.broker.close_position).parameters
            close_kwargs: dict[str, Any] = {"reason": reason}
            if qty is not None and "qty" in close_parameters:
                close_kwargs["qty"] = qty
            if strategy and "strategy" in close_parameters:
                close_kwargs["strategy"] = strategy

            order = await self.broker.close_position(symbol, **close_kwargs)
            if order is None:
                # Close failed immediately (no broker position) — release so retries work.
                self._release_pending_close(symbol, "close returned no order")
                return False

            status = getattr(getattr(order, "status", None), "value", getattr(order, "status", None))
            status_value = str(status).split(".")[-1].lower()
            if status_value in {OrderStatus.REJECTED.value, OrderStatus.CANCELED.value}:
                # A rejected/canceled order is not an in-flight close.
                # Releasing now is important because PIS must be able to retry
                # on the next bar.
                self._release_pending_close(symbol, f"close {status_value}")
                return False

            order_id = getattr(order, "id", None)
            if not order_id:
                # Without an order id the fill-drain path cannot bind execution
                # truth to this latch, so do not claim that the close succeeded.
                self._release_pending_close(symbol, "close returned no order id")
                return False

            # Preserve the caller's execution attribution on broker-generated
            # close orders.  PIS closes do not pass through submit(), so this is
            # the last durable metadata boundary before the fill is observed.
            if strategy:
                order.strategy = strategy
            if reason:
                order.reason = reason

            # Track the close order id; _drain_fills releases the guard when
            # this order reaches a terminal state. A partial fill no longer
            # latches the guard until the whole lot is gone.
            self._get_pending_close_order_ids()[symbol] = str(order_id)
            self._pending_close_qtys[symbol] = float(qty) if qty is not None else None
            self._pending_close_submitted_at[symbol] = utcnow()

            # Register the close order so fill-drain can resolve it by id.
            # Broker-initiated closes never pass through submit(), and an
            # unresolvable tracked id used to wedge the guard permanently.
            self.recent_orders.appendleft(order)
            hook = getattr(self, "_persist_submitted_close_order", None)
            if hook is not None and asyncio.iscoroutinefunction(hook):
                await hook(order)
            return True

        except Exception as exc:
            # Submission itself failed or its result became ambiguous. Release
            # both guards so a later tick can retry instead of wedging exits;
            # callers receive the broker exception rather than treating the
            # request as filled.  The PIS evaluation boundary catches this,
            # reverts decision-time scale-out state, and returns to the tick
            # without masking the ambiguity.
            self._release_pending_close(symbol, f"close submission ambiguous: {exc}")
            applog.warning("engine", f"{symbol}: close submission failed or is ambiguous: {exc}")
            raise
        finally:
            self._flattening_symbols.discard(symbol)

    def _get_pending_close_order_ids(self) -> dict[str, str]:
        """Lazily materialize the close-order-id map (test engines may skip __init__)."""
        ids = getattr(self, "_pending_close_order_ids", None)
        if ids is None:
            ids = {}
            self._pending_close_order_ids = ids
        return ids

    def _release_pending_close(self, symbol: str, cause: str) -> None:
        """Release the per-symbol close latch so future PIS exits can execute."""
        if not hasattr(self, "_pending_close_symbols"):
            return
        if symbol not in self._pending_close_symbols:
            return
        self._pending_close_symbols.discard(symbol)
        self._get_pending_close_order_ids().pop(symbol, None)
        if hasattr(self, "_pending_close_qtys"):
            self._pending_close_qtys.pop(symbol, None)
        if hasattr(self, "_pending_close_submitted_at"):
            self._pending_close_submitted_at.pop(symbol, None)
        applog.debug("engine", f"{symbol}: close-guard released ({cause})")

    async def _persist_submitted_close_order(self, order: Order) -> None:
        """Hook for persistence of submitted close orders (durable subclass)."""
        pass

    async def manual_order(self, req: OrderRequest) -> Order:
        """Submit a manual order through the risk gateway and broker."""
        if self.state != EngineState.RUNNING or self.broker is None:
            raise RuntimeError("Engine is not running")

        # Risk gateway: block manual entry orders in live mode
        positions = await self.broker.positions()
        is_entry = True
        pos = next((p for p in positions if p.symbol == req.symbol), None)
        if pos is not None and abs(pos.qty) > 1e-9:
            if pos.qty > 0:  # Long position
                if req.side == OrderSide.SELL and req.qty <= pos.qty:
                    is_entry = False  # Reducing or closing long is allowed
            else:  # Short position
                if req.side == OrderSide.BUY and req.qty <= abs(pos.qty):
                    is_entry = False  # Reducing or closing short is allowed

        if self.mode == "live" and is_entry:
            raise RuntimeError(
                f"Manual entry orders for {req.symbol} are disabled in live mode to enforce risk management. "
                f"Use automated strategy entries or close/flatten manually."
            )

        req.strategy = "manual"

        # Owner control (CR-21): per-session manual-entry budget. Closes and
        # position reductions always flow; only NEW discretionary entries
        # count against the budget. Raises like other manual gates so the API
        # surfaces it as HTTP 400 with the reason intact.
        if is_entry:
            if not hasattr(self, "_manual_entry_dates"):
                self._manual_entry_dates = []
            today_et = datetime.now(ZoneInfo("America/New_York")).date()
            session_count = sum(1 for d in self._manual_entry_dates if d == today_et)
            _rc = getattr(self, "risk_config", None)
            budget = int(getattr(_rc, "max_manual_entries_per_session", 3) if _rc is not None else 3)
            if session_count >= budget:
                applog.warning(
                    "engine",
                    f"Manual entry BLOCKED for {req.symbol}: session budget exhausted "
                    f"({session_count}/{budget}) -- operator review required",
                )
                raise RuntimeError(
                    f"Manual entry budget exhausted ({session_count}/{budget} used this session) -- "
                    f"operator review required. Closing/reducing positions is still allowed."
                )
            self._manual_entry_dates.append(today_et)
            applog.info("engine", f"Manual entry {session_count + 1}/{budget} this session: {req.side} {req.qty} {req.symbol} ({req.reason})")

        # If this is a manual sell/cover (closing or reducing), add to pending-close guard so
        # strategies cannot immediately re-enter on the same/next tick before the fill drains.
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        if not hasattr(self, "_pending_close_order_ids"):
            self._pending_close_order_ids = {}
        sym_upper = req.symbol.upper()
        if not is_entry:
            self._pending_close_symbols.add(sym_upper)

        try:
            order = await self.broker.submit(req)
        except Exception:
            # Submission failed — nothing is in flight. Release the guard so a
            # later manual attempt or strategy exit can proceed.
            self._pending_close_symbols.discard(sym_upper)
            raise

        if not is_entry:
            if order.status == OrderStatus.REJECTED:
                self._pending_close_symbols.discard(sym_upper)
            else:
                self._pending_close_order_ids[sym_upper] = str(order.id)

        self.recent_orders.appendleft(order)
        await asyncio.to_thread(self.db.save_order, order, self.mode)
        await self._drain_fills(utcnow())
        self.publish("orders", self.orders_payload())
        await self._publish_positions()
        return order

    async def close_symbol(self, symbol: str) -> Order | None:
        """Close an individual symbol's position."""
        if self.broker is None:
            raise RuntimeError("Engine is not running")

        # Guard: add to pending-close so strategies cannot immediately re-enter on the same/next tick
        sym_upper = symbol.upper()
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        if not hasattr(self, "_pending_close_order_ids"):
            self._pending_close_order_ids = {}

        self._pending_close_symbols.add(sym_upper)
        try:
            order = await self.broker.close_position(symbol, reason="manual close")
        except Exception:
            self._pending_close_symbols.discard(sym_upper)
            raise

        if order is not None:
            self._pending_close_order_ids[sym_upper] = str(order.id)
            self.recent_orders.appendleft(order)
            await asyncio.to_thread(self.db.save_order, order, self.mode)
        else:
            # Close failed — release guard so retries work
            self._pending_close_symbols.discard(sym_upper)

        await self._drain_fills(utcnow())
        await self._publish_positions()
        return order
