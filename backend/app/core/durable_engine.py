"""TradingEngine persistence hardening for the operator ledger.

This subclass changes no strategy, risk, sizing, or broker decision logic.  It
only makes order lifecycle observations durable so the dashboard can reconstruct
execution history after a process restart.
"""
from __future__ import annotations

from datetime import datetime

from app.core.domain import Order, OrderStatus, OrderType
from app.core.engine import TradingEngine
from app.core.logging import applog


class DurableTradingEngine(TradingEngine):
    """TradingEngine with durable order-history bookkeeping."""

    def _persist_observed_fill_order(self, fill) -> None:  # noqa: ANN001
        if not fill.order_id:
            return

        order = next(
            (
                candidate
                for candidate in self.recent_orders
                if candidate.id == fill.order_id or candidate.client_order_id == fill.order_id
            ),
            None,
        )
        if order is None:
            order = self.db.get_order(fill.order_id)

        if order is None:
            # Broker-driven closes (EOD/emergency/flatten) can produce a fill
            # without ever passing through the normal submit() persistence path.
            # Preserve the observed execution rather than silently losing it.
            cumulative_qty = float(getattr(fill, "cumulative_qty", 0.0) or 0.0)
            filled_qty = cumulative_qty if cumulative_qty > 0 else float(fill.qty)
            order = Order(
                id=str(fill.order_id),
                client_order_id=str(fill.order_id),
                symbol=fill.symbol,
                side=fill.side,
                qty=filled_qty,
                order_type=OrderType.MARKET,
                status=OrderStatus.FILLED,
                filled_qty=filled_qty,
                filled_avg_price=float(fill.price),
                strategy=getattr(fill, "strategy", "engine") or "engine",
                reason=getattr(fill, "reason", "broker fill") or "broker fill",
                submitted_at=fill.ts,
                filled_at=fill.ts,
                leg="exit",
            )
            self.recent_orders.appendleft(order)
        else:
            cumulative_qty = float(getattr(fill, "cumulative_qty", 0.0) or 0.0)
            order_qty = max(0.0, float(getattr(order, "qty", 0.0) or 0.0))
            order.filled_qty = cumulative_qty if cumulative_qty > 0 else max(float(order.filled_qty or 0.0), float(fill.qty))
            
            if order_qty > 0 and order.filled_qty + 1e-9 >= order_qty:
                order.status = OrderStatus.FILLED
                order.filled_at = fill.ts
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
                order.filled_at = None
                
            order.filled_avg_price = float(fill.price)

        self.db.save_order(order, self.mode)

    async def _drain_fills(self, now: datetime) -> None:
        """Run canonical fill processing, then persist the resulting order state."""
        if self.broker is None:
            return await super()._drain_fills(now)

        captured = []
        original_drain = self.broker.drain_fills

        def capture_drain():
            fills = original_drain()
            captured.extend(fills)
            return fills

        self.broker.drain_fills = capture_drain
        try:
            await super()._drain_fills(now)
        finally:
            self.broker.drain_fills = original_drain

        for fill in captured:
            try:
                self._persist_observed_fill_order(fill)
            except Exception as exc:
                # Order-history persistence must never corrupt the canonical fill
                # path. The trade/fill durability remains authoritative.
                applog.error(
                    "engine.order_ledger",
                    f"Failed to persist terminal order state for {fill.order_id}: {exc}",
                )

    async def _close_position_guarded(self, symbol: str, reason: str) -> None:
        """Canonical duplicate-close guard plus immediate close-order persistence."""
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        if symbol in self._pending_close_symbols or symbol in self._flattening_symbols:
            applog.debug("engine", f"{symbol}: skipping duplicate close — close already in-flight (#45)")
            return

        self._flattening_symbols.add(symbol)
        self._pending_close_symbols.add(symbol)
        try:
            order = await self.broker.close_position(symbol, reason=reason)
            if order is None:
                self._pending_close_symbols.discard(symbol)
                return
            self.recent_orders.appendleft(order)
            self.db.save_order(order, self.mode)
            self.publish("orders", self.orders_payload())
        finally:
            self._flattening_symbols.discard(symbol)
