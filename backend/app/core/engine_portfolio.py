"""TradingEnginePortfolioMixin mixin."""

from __future__ import annotations
import asyncio
import json
from datetime import datetime, timedelta, timezone, UTC
from typing import Any
import pandas as pd
from app.core.domain import Signal, SignalAction, Position, OrderRequest, OrderSide, OrderType, Order, OrderRole
from app.core.applog import applog
from app.indicators.ta import enrich
from app.ai.regime import detect_regime
from app.strategies import SignalPipeline, StrategyContext

class TradingEnginePortfolioMixin:
    async def _close_position_guarded(self, symbol: str, reason: str, qty: float | None = None, strategy: str | None = None, role: OrderRole | None = None) -> Order | None:
        """Close a position, skipping if a close order is already in-flight for this symbol."""
        import time
        if not hasattr(self, "_pending_close_ttls"):
            self._pending_close_ttls = {}

        now = time.time()
        # fix(#45): guard against concurrent per-tick flatten submissions, with a 30s TTL
        # to handle ambiguous broker states without double-submitting
        if symbol in self._pending_close_ttls and now < self._pending_close_ttls[symbol]:
            applog.debug("engine", f"{symbol}: skipping duplicate close — close already in-flight (#45)")
            return

        self._flattening_symbols.add(symbol)
        self._pending_close_ttls[symbol] = now + 30.0  # Lock out retries for 30 seconds
        order = None
        try:
            if qty is not None:
                try:
                    order = await self.broker.close_position(symbol, reason=reason, qty=qty, strategy=strategy, role=role)
                except TypeError:
                    order = await self.broker.close_position(symbol, reason=reason, strategy=strategy, role=role)
            else:
                order = await self.broker.close_position(symbol, reason=reason, strategy=strategy, role=role)

            # Ambiguous timeouts are intentionally covered by the TTL because the broker may have accepted
            # the order even though the client did not receive a definitive response.
            return order
        finally:
            self._flattening_symbols.discard(symbol)

    def _reconcile_positions(self, positions: list[Position]) -> None:
        """Synchronously reconcile local lots to authoritative broker positions.

        This method is intentionally synchronous because all current callers invoke it without
        awaiting. The database calls used here are synchronous, and making this method async caused
        the coroutine to be silently dropped, leaving broker/local/PIS state unreconciled.
        """
        broker_symbols = {p.symbol for p in positions if abs(float(p.qty)) > 1e-9}
        for local_symbol in list(self.portfolio._lots):
            if local_symbol not in broker_symbols:
                self.portfolio._lots.pop(local_symbol, None)
                self._position_take_profits.pop(local_symbol, None)
                self._position_peaks.pop(local_symbol, None)

        for p in positions:
            existing_lot = self.portfolio._lots.get(p.symbol)
            if existing_lot is not None:
                qty_matches = abs(float(existing_lot.qty) - float(p.qty)) <= 1e-9
                price_scale = max(1.0, abs(float(p.avg_entry_price)))
                avg_matches = abs(float(existing_lot.avg_price) - float(p.avg_entry_price)) <= 1e-8 * price_scale
                if qty_matches and avg_matches:
                    # Fill processing already established the same economic lot. Keep
                    # its original/current stop, initial R, strategy and entry timestamp.
                    p.strategy = existing_lot.strategy
                    continue

            strat = "manual"
            if hasattr(self.broker, "_symbol_strategies"):
                db_strat = self.db.get_latest_entry_order_strategy(p.symbol, self.mode)
                if db_strat:
                    self.broker._symbol_strategies[p.symbol] = db_strat
                    strat = db_strat
                    applog.info("engine", f"Restored strategy '{db_strat}' for active position {p.symbol}")

            # Position ownership comes from the latest true ENTRY, never from a
            # newer emergency/protective order.
            entry_payload = self.db.get_latest_entry_order_payload(p.symbol, self.mode)
            if not isinstance(entry_payload, dict):
                entry_payload = None
            fallback_payload = self.db.get_latest_order_payload(p.symbol, self.mode)
            if not isinstance(fallback_payload, dict):
                fallback_payload = None
            order_payload = entry_payload or fallback_payload
            if entry_payload and entry_payload.get("strategy"):
                strat = str(entry_payload["strategy"])
                p.strategy = strat
                if hasattr(self.broker, "_symbol_strategies"):
                    self.broker._symbol_strategies[p.symbol] = strat

            stop_loss = None
            take_profit = None
            initial_risk = None
            opened_at = p.opened_at if hasattr(p, "opened_at") and p.opened_at else datetime.now(UTC)
            if order_payload:
                stop_loss = order_payload.get("stop_loss")
                take_profit = order_payload.get("take_profit")
                try:
                    stop_loss = float(stop_loss) if stop_loss is not None else None
                except (TypeError, ValueError):
                    stop_loss = None
                try:
                    take_profit = float(take_profit) if take_profit is not None else None
                except (TypeError, ValueError):
                    take_profit = None
                if stop_loss and stop_loss > 0:
                    initial_risk = abs(p.avg_entry_price - stop_loss)
                if take_profit is not None and float(take_profit) > 0:
                    self._position_take_profits[p.symbol] = float(take_profit)
                    p.take_profit = float(take_profit)
                submitted_at_str = order_payload.get("submitted_at")
                if submitted_at_str:
                    try:
                        opened_at = datetime.fromisoformat(submitted_at_str)
                    except Exception:
                        pass

            # Local DB stop metadata is not proof of authoritative broker protection.
            if stop_loss is None or stop_loss <= 0:
                applog.error(
                    "engine.risk",
                    f"CRITICAL: Position {p.symbol} has no authoritative broker-side stop-loss! Flattening immediately."
                )
                asyncio.create_task(
                    self._close_position_guarded(
                        p.symbol,
                        reason="CRITICAL: Missing broker-side stop-loss",
                        qty=abs(p.qty),
                    )
                )
                stop_loss = 0.0
                initial_risk = 0.0

            from app.portfolio.tracker import _OpenLot
            self.portfolio._lots[p.symbol] = _OpenLot(
                qty=p.qty,
                avg_price=p.avg_entry_price,
                entry_ts=opened_at,
                strategy=strat,
                stop_loss=stop_loss,
                initial_risk_per_share=initial_risk,
            )
            applog.info(
                "engine",
                f"Reconstructed portfolio lot for {p.symbol}: qty={p.qty}, "
                f"avg_price={p.avg_entry_price}, strategy={strat}, stop_loss={stop_loss}",
            )
        self.reconciliation_failed = False
