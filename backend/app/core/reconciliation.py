"""TradingEngineReconciliationMixin.

Service boundary extracting position reconciliation, ambiguous order resolution,
and bi-directional orphan auto-adopt/drain latch from TradingEngine.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.applog import applog
from app.core.domain import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    utcnow,
)


class TradingEngineReconciliationMixin:
    """Position and order reconciliation mixin for TradingEngine."""

    async def _reconcile_pending_closes(self, now: datetime) -> None:
        """Per-tick reconciliation of pending close latches.

        The legacy release path only ran when a NEW fill arrived for the same
        symbol. After a partial scale-out the residual lot generates no further
        fills until EOD, so a guard whose tracked order could not be resolved
        stayed latched for the rest of the session and silently swallowed every
        PIS exit (2026-08-24 CRWV/V/AMZN). Re-check liveness every tick from
        three sources: tracked-order terminal state, fill-truth completion, and
        a staleness deadline.
        """
        pending = getattr(self, "_pending_close_symbols", None)
        if not pending:
            return

        stale_after = 90.0  # seconds; ~2 evaluation ticks even under degraded data
        for symbol in list(pending):
            tracked_id = self._get_pending_close_order_ids().get(symbol)

            # 1) Tracked order reached a terminal state?
            if tracked_id:
                resolved = next(
                    (o for o in self.recent_orders if str(o.id) == tracked_id or o.client_order_id == tracked_id),
                    None,
                )
                if resolved is not None:
                    status = getattr(resolved.status, "value", resolved.status)
                    if str(status).upper() in {"FILLED", "CANCELED", "REJECTED"}:
                        self._release_pending_close(symbol, f"order terminal: {status}")
                        continue

            # 2) Latch older than the staleness deadline? Force-release; PIS
            #    re-evaluates on the next bar and will re-close if still needed.
            submitted_at = getattr(self, "_pending_close_submitted_at", {}).get(symbol)
            if submitted_at is not None and (now - submitted_at).total_seconds() > stale_after:
                applog.warning(
                    "engine",
                    f"{symbol}: close-guard force-released after {stale_after:.0f}s staleness — "
                    "prior close never confirmed; PIS may retry",
                )
                self._release_pending_close(symbol, "stale latch")

    def _reconcile_positions(self, positions: list[Position]) -> None:
        """Reconcile local lots to authoritative broker positions without erasing matching lot state."""
        _QTY_TOL = 1e-4  # fractional-share/rounding tolerance; 1e-9 caused permanent mismatch thrash
        broker_symbols = {p.symbol for p in positions if abs(float(p.qty)) > _QTY_TOL}

        for local_symbol in list(self.portfolio._lots):
            if local_symbol not in broker_symbols:
                self.portfolio._lots.pop(local_symbol, None)
                self._position_take_profits.pop(local_symbol, None)
                self._position_peaks.pop(local_symbol, None)
                self._position_troughs.pop(local_symbol, None)
                self._position_initial_stops.pop(local_symbol, None)

        for p in positions:
            existing_lot = self.portfolio._lots.get(p.symbol)
            if existing_lot is not None:
                qty_matches = abs(float(existing_lot.qty) - float(p.qty)) <= _QTY_TOL
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
            # newer emergency/protective order.  This keeps UI attribution and
            # strategy-specific exit management bound to the strategy that opened
            # the surviving position.
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
                stop_loss_val = order_payload.get("stop_loss")
                take_profit = order_payload.get("take_profit")
                take_profit_val = take_profit
                try:
                    stop_loss = float(stop_loss_val) if stop_loss_val is not None else None
                except (TypeError, ValueError):
                    stop_loss = None
                try:
                    take_profit = float(take_profit_val) if take_profit_val is not None else None
                except (TypeError, ValueError):
                    take_profit = None
                if stop_loss and stop_loss > 0:
                    initial_risk = abs(p.avg_entry_price - stop_loss)

                # Exempt runner strategies from DB take-profit injection
                runner_strats = {"orb", "momentum", "trend_pullback", "bb_squeeze", "gap_and_go", "relative_strength"}
                if strat not in runner_strats and take_profit is not None and float(take_profit) > 0:
                    self._position_take_profits[p.symbol] = float(take_profit)
                    p.take_profit = float(take_profit)
                elif strat in runner_strats:
                    p.take_profit = None
                    self._position_take_profits.pop(p.symbol, None)

                submitted_at_str = order_payload.get("submitted_at")
                if submitted_at_str:
                    try:
                        opened_at = datetime.fromisoformat(submitted_at_str)
                    except Exception:
                        pass

            # Restore local stop/risk metadata for lot tracking and PIS. This
            # fallback does not submit or verify a protective broker order.
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

    async def _reconcile_pending_submissions(self, now: datetime | None = None) -> None:
        """Resolve ambiguous submits without releasing duplicate/exposure guards early."""
        if not self.pending_reconciliation_orders:
            return

        now = now or utcnow()
        min_absence_age_seconds = 60.0
        required_not_found_observations = 3
        client_ids = list(self.pending_reconciliation_orders.keys())
        applog.info("engine", f"Checking status of {len(client_ids)} ambiguous order submissions: {client_ids}")
        changed = False

        for cid in client_ids:
            order_info = self.pending_reconciliation_orders.get(cid)
            if not isinstance(order_info, dict):
                continue
            symbol = str(order_info.get("symbol", "")).upper()
            try:
                order = await self.broker.get_order_by_client_id(cid)
            except Exception as exc:
                applog.warning("engine", f"Reconciliation query failed for client ID {cid}: {exc}. Retaining guard.")
                continue

            if order is not None:
                applog.info("engine", f"Ambiguous order resolved from broker truth: {symbol} ({cid}) status={order.status}.")
                self.pending_reconciliation_orders.pop(cid, None)
                recovered_target = order_info.get("take_profit")
                if recovered_target is not None:
                    try:
                        recovered_target = float(recovered_target)
                    except (TypeError, ValueError):
                        recovered_target = None
                if recovered_target is not None and recovered_target > 0:
                    self._position_take_profits[symbol] = recovered_target
                    order = order.model_copy(update={"take_profit": recovered_target})
                await asyncio.to_thread(self.db.save_order, order, self.mode)
                changed = True
                continue

            submitted_at = order_info.get("submitted_at")
            try:
                submitted_dt = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
                if submitted_dt.tzinfo is None:
                    submitted_dt = submitted_dt.replace(tzinfo=UTC)
                age_seconds = max(0.0, (now - submitted_dt.astimezone(UTC)).total_seconds())
            except (TypeError, ValueError):
                age_seconds = 0.0

            count = int(order_info.get("reconcile_not_found_count", 0) or 0) + 1
            order_info["reconcile_not_found_count"] = count
            order_info["last_reconcile_not_found_at"] = now.isoformat()
            if not order_info.get("first_reconcile_not_found_at"):
                order_info["first_reconcile_not_found_at"] = now.isoformat()
            changed = True

            if count < required_not_found_observations or age_seconds < min_absence_age_seconds:
                applog.warning("engine", f"Ambiguous order still unresolved: {symbol} ({cid}) broker_not_found observation={count}/{required_not_found_observations} age={age_seconds:.1f}s; retaining guard.")
                continue

            applog.warning("engine", f"Ambiguous order resolved absent after bounded reconciliation: {symbol} ({cid}) not_found_observations={count} age={age_seconds:.1f}s. Treating as REJECTED.")
            self.pending_reconciliation_orders.pop(cid, None)
            rejected_order = Order(
                id=cid,
                client_order_id=cid,
                symbol=symbol,
                side=OrderSide.BUY if str(order_info.get("side", "")).lower() == "buy" else OrderSide.SELL,
                qty=float(order_info.get("qty", 0.0) or 0.0),
                order_type=OrderType.LIMIT if str(order_info.get("order_type", "")).lower() == "limit" else OrderType.MARKET,
                limit_price=order_info.get("limit_price"),
                status=OrderStatus.REJECTED,
                reason=f"Ambiguous submit resolved absent after repeated broker not-found observations ({count}) and {age_seconds:.1f}s age.",
            )
            await asyncio.to_thread(self.db.save_order, rejected_order, self.mode)

        if changed:
            await asyncio.to_thread(self.db.set_setting, "pending_reconciliation_orders", self.pending_reconciliation_orders)

    async def _evaluate_position_reconciliation(
        self,
        broker_positions: list[Position],
        lots: dict[str, Any],
        open_orders: list[Any],
        diagnostics: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Evaluate broker vs portfolio lots, track mismatch streaks, and trigger bi-directional orphan adopt/drain."""
        broker_syms = {
            p.symbol: float(p.qty)
            for p in broker_positions
            if hasattr(p, "symbol") and abs(float(p.qty)) > 1e-9
        }
        db_syms = (
            {
                sym: float(lot.qty)
                for sym, lot in lots.items()
                if hasattr(lot, "qty") and abs(float(lot.qty)) > 1e-9
            }
            if isinstance(lots, dict)
            else {}
        )

        position_bad = broker_syms != db_syms
        if position_bad:
            diagnostics["positions_reconciled"] = False
            mismatched_syms = {
                s for s in set(broker_syms) | set(db_syms)
                if abs(broker_syms.get(s, 0.0) - db_syms.get(s, 0.0)) > 1e-6
            }
            in_flight_syms = {
                getattr(o, "symbol", "").upper()
                for o in open_orders
                if getattr(o, "symbol", "")
            }
            unexplained_mismatches = mismatched_syms - in_flight_syms

            if unexplained_mismatches:
                self._position_reconciliation_mismatch_streak += 1
                applog.warning(
                    "engine.heartbeat",
                    f"Position reconciliation mismatch confirmation {self._position_reconciliation_mismatch_streak}/2. "
                    f"Broker: {broker_syms}, DB lots: {db_syms} (unexplained: {unexplained_mismatches})",
                )

                if self._position_reconciliation_mismatch_streak >= 3:
                    if hasattr(self.broker, "poll_fills"):
                        try:
                            await self.broker.poll_fills()
                        except Exception:
                            pass

                    # Case 1: Ghost DB lots (closed at broker, but lingered locally)
                    absent_at_broker = {s for s in unexplained_mismatches if s in db_syms and s not in broker_syms}
                    if absent_at_broker:
                        self._reconcile_positions(broker_positions)
                        for sym in absent_at_broker:
                            applog.warning(
                                "engine.reconciliation",
                                f"WARN_ORPHAN_DRAINED: Local ghost lot {sym} drained from portfolio lots; broker confirmed 0 position.",
                            )
                            if hasattr(self, "orphan_reconciliation_events") and isinstance(self.orphan_reconciliation_events, list):
                                self.orphan_reconciliation_events.append({
                                    "timestamp": now.isoformat(),
                                    "event": "WARN_ORPHAN_DRAINED",
                                    "symbol": sym,
                                    "details": "Local ghost lot drained from portfolio lots; broker confirmed 0 position.",
                                })
                        applog.info("engine.heartbeat", f"Broker truth position reconciliation restored for {absent_at_broker}; clearing stale local lots.")

                    # Case 2: Quantity mismatch on active positions (e.g. stock split or partial fill divergence)
                    qty_mismatch = {s for s in unexplained_mismatches if s in broker_syms and s in db_syms}
                    if qty_mismatch:
                        self._reconcile_positions(broker_positions)
                        applog.warning(
                            "engine.heartbeat",
                            f"Broker truth quantity resynchronized for {qty_mismatch}: "
                            f"broker={[broker_syms[s] for s in qty_mismatch]}, DB lots={[db_syms[s] for s in qty_mismatch]}.",
                        )

                    # Case 3: Orphan positions held at broker with NO local lot (e.g. split remnants or external trades)
                    orphan_at_broker = {s for s in unexplained_mismatches if s in broker_syms and s not in db_syms}
                    if orphan_at_broker:
                        self._reconcile_positions(broker_positions)
                        for orphan_sym in orphan_at_broker:
                            lot = getattr(self.portfolio, "_lots", {}).get(orphan_sym) if self.portfolio else None
                            stop_val = getattr(lot, "stop_loss", None) if lot else "N/A"
                            applog.warning(
                                "engine.reconciliation",
                                f"WARN_ORPHAN_ADOPTED: Broker orphan {orphan_sym} (qty={broker_syms.get(orphan_sym)}) adopted into active portfolio lots with protective stop {stop_val}.",
                            )
                            if hasattr(self, "orphan_reconciliation_events") and isinstance(self.orphan_reconciliation_events, list):
                                self.orphan_reconciliation_events.append({
                                    "timestamp": now.isoformat(),
                                    "event": "WARN_ORPHAN_ADOPTED",
                                    "symbol": orphan_sym,
                                    "qty": broker_syms.get(orphan_sym),
                                    "stop_loss": stop_val,
                                    "details": f"Broker orphan adopted into active portfolio lots with protective stop {stop_val}.",
                                })
                            applog.error(
                                "engine.heartbeat",
                                f"CRITICAL: Orphan position at broker detected for {orphan_sym} (broker holds {broker_syms.get(orphan_sym)} with no active DB lot). "
                                f"Initiating immediate emergency guarded liquidation to prevent unmanaged overnight exposure."
                            )
                            try:
                                await self._close_position_guarded(orphan_sym, reason="orphan_broker_reconciliation_liquidation")
                                applog.warning(
                                    "engine.reconciliation",
                                    f"WARN_ORPHAN_DRAINED: Broker orphan {orphan_sym} drained via emergency guarded liquidation.",
                                )
                                if hasattr(self, "orphan_reconciliation_events") and isinstance(self.orphan_reconciliation_events, list):
                                    self.orphan_reconciliation_events.append({
                                        "timestamp": now.isoformat(),
                                        "event": "WARN_ORPHAN_DRAINED",
                                        "symbol": orphan_sym,
                                        "details": "Broker orphan drained via emergency guarded liquidation.",
                                    })
                            except Exception as exc:
                                applog.error("engine.heartbeat", f"Failed to liquidate orphan position {orphan_sym}: {exc}")
            else:
                applog.info(
                    "engine.heartbeat",
                    f"Transient position delta for {mismatched_syms} accounted for by in-flight open orders; awaiting fill completion",
                )
        else:
            self._position_reconciliation_mismatch_streak = 0

        return position_bad
