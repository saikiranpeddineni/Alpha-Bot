"""TradingEnginePortfolioMixin mixin."""

from __future__ import annotations
import asyncio
import json
from datetime import datetime, timedelta, timezone, UTC
from typing import Any
import pandas as pd
from app.core.domain import Signal, SignalAction, Position, OrderRequest, OrderSide, OrderType, Order
from app.core.logging import applog
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
            order = await self.broker.close_position(symbol, reason=reason, qty=qty)
            # If close failed immediately (None returned), release the guard so retries work
            if order is None:
                self._pending_close_symbols.discard(symbol)
        finally:
            self._flattening_symbols.discard(symbol)
            if qty is not None:
                self._pending_close_symbols.discard(symbol)


    async def _manage_live_winner_exit(self, symbol: str, position: Any, df: pd.DataFrame, decision: Any) -> bool:
        """Promote/ratchet a proven LIVE winner using PIS single discretionary exit authority."""
        if self.mode != "live" or self.broker is None or len(df) < 2 or decision is None:
            return symbol in self._live_runner_symbols
        replace_leg = getattr(self.broker, "replace_exit_leg", None)
        if replace_leg is None:
            return False

        lot = self.portfolio._lots.get(symbol) if self.portfolio else None
        initial_stop = getattr(lot, "initial_stop_loss", None) if lot is not None else None
        if initial_stop is None:
            initial_stop = self._position_initial_stops.get(symbol)
        original_tp = self._position_take_profits.get(symbol)
        current_stop = getattr(lot, "stop_loss", None) if lot is not None else self._live_runner_floor.get(symbol)

        desired_floor = decision.target_stop_loss
        if desired_floor is None or desired_floor <= 0:
            return symbol in self._live_runner_symbols

        orders = await self.broker.open_orders()
        exits = [o for o in orders if o.symbol.upper() == symbol and o.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}]
        stop_leg = next((o for o in exits if o.leg == "stop" or o.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}), None)
        tp_leg = next((o for o in exits if o.leg == "take_profit" and o.limit_price is not None), None)
        if stop_leg is None or tp_leg is None:
            return symbol in self._live_runner_symbols

        entry = float(position.avg_entry_price)
        risk = abs(entry - float(initial_stop or entry * 0.98))
        side = 1.0 if position.qty > 0 else -1.0
        mfe_r = decision.peak_r
        target_r = max(0.1, side * (float(original_tp or entry * 1.05) - entry) / max(1e-6, risk))

        runner_tp_r = max(20.0, mfe_r + 10.0, target_r + 10.0)
        desired_tp = entry + side * runner_tp_r * risk
        tp_needs_move = (side > 0 and float(tp_leg.limit_price) < desired_tp - 0.01) or (
            side < 0 and float(tp_leg.limit_price) > desired_tp + 0.01
        )
        if tp_needs_move:
            replaced_tp = await replace_leg(tp_leg.id, limit_price=desired_tp)
            if replaced_tp is None:
                return symbol in self._live_runner_symbols

        stop_px = float(stop_leg.stop_price or 0.0)
        stop_needs_move = (side > 0 and desired_floor > stop_px + 0.01) or (
            side < 0 and (stop_px <= 0 or desired_floor < stop_px - 0.01)
        )
        if stop_needs_move:
            replaced_stop = await replace_leg(stop_leg.id, stop_price=desired_floor)
            if replaced_stop is None:
                return symbol in self._live_runner_symbols
            confirmed_floor = float(replaced_stop.stop_price or desired_floor)
            self._live_runner_floor[symbol] = confirmed_floor
            if self.portfolio:
                self.portfolio.note_stop(symbol, confirmed_floor)
        elif stop_px > 0:
            self._live_runner_floor[symbol] = stop_px

        floor = self._live_runner_floor.get(symbol)
        protected_profit = floor is not None and ((side > 0 and floor > entry) or (side < 0 and floor < entry))
        if protected_profit:
            if symbol not in self._live_runner_symbols:
                applog.info(
                    "engine.exit",
                    f"{symbol}: LIVE RUNNER confirmed mfe={mfe_r:.2f}R floor={floor:.4f} via PIS; original TP/ordinary strategy exits subordinated",
                )
            self._live_runner_symbols.add(symbol)
        return symbol in self._live_runner_symbols

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
            opened_at = p.opened_at if hasattr(p, "opened_at") and p.opened_at else utcnow()
            if order_payload:
                stop_loss = order_payload.get("stop_loss")
                take_profit = order_payload.get("take_profit")
                try:
                    stop_loss = float(stop_loss) if stop_loss is not None and not isinstance(stop_loss, MagicMock) else None
                except (TypeError, ValueError):
                    stop_loss = None
                try:
                    take_profit = float(take_profit) if take_profit is not None and not isinstance(take_profit, MagicMock) else None
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
                stop_pct = 0.015  # 1.5% conservative protective fallback
                if p.qty > 0:
                    stop_loss = round(p.avg_entry_price * (1.0 - stop_pct), 4)
                else:
                    stop_loss = round(p.avg_entry_price * (1.0 + stop_pct), 4)
                initial_risk = abs(p.avg_entry_price - stop_loss)

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

