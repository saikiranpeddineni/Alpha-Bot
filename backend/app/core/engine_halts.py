"""TradingEngineHaltsMixin mixin."""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.core.applog import applog
from app.core.domain import (
    EngineState,
    utcnow,
)


class TradingEngineHaltsMixin:
    # =============================================================================
    # [MODIFIED 2026-10-27]: Decommission Emergency Flatten Loop
    # ORIGINAL INTENT: Ran every 10s flattening all positions when engine in HALTED state.
    # REASON FOR CHANGE: Prevent engine from wiping out active positions when in halted state.
    # SAFE ROLLBACK: Previous code preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # async def _emergency_flatten_loop(self) -> None:
    #     applog.error("engine", "EMERGENCY: Starting emergency flatten loop due to engine failure.")
    #     attempts = 0
    #     while self.state == EngineState.HALTED:
    #         try:
    #             if self.broker is None:
    #                 break
    #             positions = await self.broker.positions()
    #             open_orders = await self.broker.open_orders()
    #             if not positions and not open_orders:
    #                 applog.info("engine", "Emergency flatten successful: no active positions or open orders remaining.")
    #                 break
    #             if open_orders:
    #                 applog.warning(
    #                     "engine",
    #                     f"Emergency flatten: Cancelling {len(open_orders)} open orders (attempt {attempts+1})..."
    #                 )
    #                 await self.broker.cancel_all()
    #             if positions:
    #                 applog.warning(
    #                     "engine",
    #                     f"Emergency flatten: Flattening {len(positions)} active positions (attempt {attempts+1})..."
    #                 )
    #                 await self.broker.flatten_all(reason="fatal engine halt (retry)")
    #             await self._drain_fills(utcnow())
    #         except Exception as exc:
    #             applog.error("engine", f"Emergency flatten attempt {attempts+1} failed: {exc}")
    #         attempts += 1
    #         await asyncio.sleep(10.0)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    async def _emergency_flatten_loop(self) -> None:
        return
    # =============================================================================

    async def _bank_profit_lock(self, now: datetime) -> bool:
        """PROFIT LOCK 2026-10-16 (bank-and-ride): scratch underwater positions,
        keep green runners under PIS. New entries are already blocked by the
        lock; this removes the dead weight so the banked day cannot be dragged
        red by losers. Winners keep their broker stop legs and PIS floors.
        Returns True when no underwater positions remain.
        """
        assert self.broker is not None
        try:
            positions = await self.broker.positions()
        except Exception as exc:
            applog.warning("engine", f"Profit-lock banking: position fetch failed: {exc}")
            return False
        scratched = 0
        for position in positions:
            try:
                upnl = float(position.unrealized_pnl)
            except Exception:
                continue
            if upnl <= 0:
                try:
                    await self.broker.close_position(
                        position.symbol, reason="daily profit lock (scratch loser)"
                    )
                    scratched += 1
                except Exception as exc:  # noqa: BLE001
                    applog.warning("engine", f"Profit-lock scratch failed for {position.symbol}: {exc}")
        try:
            await self._drain_fills(now)
        except Exception:
            pass
        applog.warning("engine", f"PROFIT LOCK banking: scratched {scratched} underwater positions; winners ride")
        # NOTE: PIS brains are deliberately NOT purged here (unlike the loss
        # halt path) — green runners keep their excursion memory and floors.
        self.publish("status", self.status())
        return True

    async def _reconcile_daily_halt_liquidation(self, now: datetime) -> bool:
        """Keep daily halt liquidation active until broker state proves flat."""
        assert self.broker is not None
        import asyncio
        try:
            open_orders = await self.broker.open_orders()
            if open_orders:
                applog.warning("engine", f"Daily halt liquidation: cancelling {len(open_orders)} open orders")
                await self.broker.cancel_all()
            positions = await self.broker.positions()
            if positions:
                applog.warning("engine", f"Daily halt liquidation unresolved: flattening {len(positions)} authoritative positions")
                await self.broker.flatten_all(reason="daily loss halt")
                await self._drain_fills(now)
            residual_positions = await self.broker.positions()
            residual_orders = await self.broker.open_orders()
            if residual_positions or residual_orders:
                symbols = sorted({position.symbol for position in residual_positions})
                self.last_error = (
                    "DAILY LOSS HALTED — liquidation unresolved: "
                    f"residual_positions={symbols}, open_orders={len(residual_orders)}"
                )
                applog.error("engine", self.last_error)
                self.publish("status", self.status())
                return False
            if self.last_error.startswith("DAILY LOSS HALTED — liquidation unresolved"):
                self.last_error = ""
            applog.info("engine", "Daily halt liquidation authoritatively complete: flat")
            # Purge PIS brains so stale excursion memory cannot contaminate the next trade.
            if hasattr(self, "pis_adapter") and self.pis_adapter and hasattr(self.pis_adapter, "service"):
                self.pis_adapter.service.close_all()
            self.publish("status", self.status())
            return True
        except BaseException as exc:
            self.last_error = f"DAILY LOSS HALTED — liquidation unresolved: {type(exc).__name__}: {exc}"
            applog.error("engine", self.last_error)
            self.publish("status", self.status())
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return False

    async def _reconcile_eod_liquidation(self, session, now: datetime) -> bool:  # noqa: ANN001
        """Retry EOD liquidation until the broker proves positions and orders are clear."""
        assert self.broker is not None
        try:
            open_orders = await self.broker.open_orders()
            if open_orders:
                applog.warning("engine", f"EOD liquidation: cancelling {len(open_orders)} open orders before flatten retry")
                await self.broker.cancel_all()
            positions = await self.broker.positions()
            if positions:
                applog.warning("engine", f"EOD liquidation unresolved: flattening {len(positions)} authoritative positions")
                if not hasattr(self, "_pending_close_symbols"):
                    self._pending_close_symbols = set()
                for position in positions:
                    self._pending_close_symbols.add(position.symbol.upper())
                await self.broker.flatten_all(reason="end of day")

                for _ in range(10):
                    await asyncio.sleep(1.0)
                    await self._drain_fills(now)
                    if not await self.broker.positions() and not await self.broker.open_orders():
                        break

            residual_positions = await self.broker.positions()
            residual_orders = await self.broker.open_orders()
            if residual_positions or residual_orders:
                symbols = sorted({position.symbol for position in residual_positions})
                self.last_error = (
                    "CRITICAL EOD liquidation unresolved: "
                    f"residual_positions={symbols}, open_orders={len(residual_orders)}"
                )
                applog.error("engine", self.last_error)
                self.publish("status", self.status())
                return False
            self._flattened_sessions.add(session)
            getattr(self, "_eod_flatten_started_sessions", set()).discard(session)
            if self.last_error.startswith("CRITICAL EOD liquidation unresolved"):
                self.last_error = ""
            applog.info("engine", "End-of-day liquidation authoritatively complete")
            # Explicitly clear out all PIS memory states for the next session
            if hasattr(self, "pis_adapter") and self.pis_adapter and hasattr(self.pis_adapter, "service"):
                self.pis_adapter.service.close_all()
            self.publish("status", self.status())
            return True
        except BaseException as exc:
            self.last_error = f"CRITICAL EOD liquidation unresolved: {type(exc).__name__}: {exc}"
            applog.error("engine", self.last_error)
            self.publish("status", self.status())
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return False

    async def _reconcile_hard_halt_liquidation(self, now: datetime) -> bool:
        """Keep a latched hard halt unresolved until broker state proves flat."""
        assert self.broker is not None
        try:
            open_orders = await self.broker.open_orders()
            if open_orders:
                applog.warning("engine", f"Hard halt liquidation: cancelling {len(open_orders)} open orders")
                await self.broker.cancel_all()
            positions = await self.broker.positions()
            if positions:
                applog.warning("engine", f"Hard halt liquidation unresolved: flattening {len(positions)} authoritative positions")
                await self.broker.flatten_all(reason="max drawdown kill switch")
                await self._drain_fills(now)
            residual_positions = await self.broker.positions()
            residual_orders = await self.broker.open_orders()
            if residual_positions or residual_orders:
                symbols = sorted({position.symbol for position in residual_positions})
                self.last_error = (
                    "HARD HALTED — liquidation unresolved: "
                    f"residual_positions={symbols}, open_orders={len(residual_orders)}"
                )
                applog.error("engine", self.last_error)
                self.publish("status", self.status())
                return False
            if self.last_error.startswith("HARD HALTED — liquidation unresolved"):
                self.last_error = "HARD HALTED — flat"
            applog.info("engine", "Hard halt liquidation authoritatively complete: flat")
            # Purge PIS brains so stale excursion memory cannot contaminate the next trade.
            if hasattr(self, "pis_adapter") and self.pis_adapter and hasattr(self.pis_adapter, "service"):
                self.pis_adapter.service.close_all()
            self.publish("status", self.status())
            return True
        except BaseException as exc:
            self.last_error = f"HARD HALTED — liquidation unresolved: {type(exc).__name__}: {exc}"
            applog.error("engine", self.last_error)
            self.publish("status", self.status())
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return False

    async def flatten(self) -> None:
        if self.broker is None:
            raise RuntimeError("Engine is not running")
        await self.broker.flatten_all(reason="manual flatten")
        await self._drain_fills(utcnow())
        await self._publish_positions()

