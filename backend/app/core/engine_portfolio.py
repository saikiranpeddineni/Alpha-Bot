"""TradingEnginePortfolioMixin mixin."""

from __future__ import annotations
import asyncio
import json
from datetime import datetime, timedelta, timezone, UTC
from typing import Any
import pandas as pd
from app.core.domain import Signal, SignalAction, Position, OrderRequest, OrderSide, OrderType, Order
from app.core.applog import applog
from app.indicators.ta import enrich
from app.ai.regime import detect_regime
from app.strategies import SignalPipeline, StrategyContext

class TradingEnginePortfolioMixin:
    async def _close_position_guarded(self, symbol: str, reason: str, qty: float | None = None) -> None:
        """Close a position, skipping if a close order is already in-flight for this symbol."""
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        # fix(#45): also guard against concurrent per-tick flatten submissions
        if symbol in self._pending_close_symbols or symbol in self._flattening_symbols:
            applog.debug("engine", f"{symbol}: skipping duplicate close — close already in-flight (#45)")
            return
        self._flattening_symbols.add(symbol)
        self._pending_close_symbols.add(symbol)
        try:
            if qty is not None:
                try:
                    order = await self.broker.close_position(symbol, reason=reason, qty=qty)
                except TypeError:
                    order = await self.broker.close_position(symbol, reason=reason)
            else:
                order = await self.broker.close_position(symbol, reason=reason)
            # If close failed immediately (None returned), release the guard so retries work
            if order is None:
                self._pending_close_symbols.discard(symbol)
        finally:
            self._flattening_symbols.discard(symbol)
            if qty is not None:
                self._pending_close_symbols.discard(symbol)



    async def _reconcile_positions(self, positions: list[Position]) -> None:
        """Reconcile local lots to authoritative broker positions without erasing matching lot state."""
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
                db_strat = await asyncio.to_thread(self.db.get_latest_entry_order_strategy, p.symbol, self.mode)
                if db_strat:
                    self.broker._symbol_strategies[p.symbol] = db_strat
                    strat = db_strat
                    applog.info("engine", f"Restored strategy '{db_strat}' for active position {p.symbol}")

            # Position ownership comes from the latest true ENTRY, never from a
            # newer emergency/protective order.  This keeps UI attribution and
            # strategy-specific exit management bound to the strategy that opened
            # the surviving position.
            entry_payload = await asyncio.to_thread(self.db.get_latest_entry_order_payload, p.symbol, self.mode)
            if not isinstance(entry_payload, dict):
                entry_payload = None
            fallback_payload = await asyncio.to_thread(self.db.get_latest_order_payload, p.symbol, self.mode)
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
                # F-14 FIX: Use try/float() instead of isinstance(x, MagicMock) guards.
                # The MagicMock check was the only reason unittest.mock was imported in
                # production code. A safe float conversion handles all non-numeric values
                # (including mock objects in tests) without test infrastructure in the
                # production import graph.
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

            # fix(#41): local DB stop_loss metadata is stored for initial lot tracking,
            # but MUST NOT count as proof of authoritative broker-side protection.
            if stop_loss is None or stop_loss <= 0:
                applog.error("engine.risk", f"CRITICAL: Position {p.symbol} has no authoritative broker-side stop-loss! Flattening immediately.")
                asyncio.create_task(self._close_position_guarded(p.symbol, reason="CRITICAL: Missing broker-side stop-loss", qty=abs(p.qty)))
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

