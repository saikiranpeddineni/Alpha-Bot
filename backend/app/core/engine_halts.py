"""TradingEngineHaltsMixin mixin."""

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

class TradingEngineHaltsMixin:
    async def _emergency_flatten_loop(self) -> None:
        applog.error("engine", "EMERGENCY: Starting emergency flatten loop due to engine failure.")
        attempts = 0
        while self.state == EngineState.HALTED:
            try:
                if self.broker is None:
                    break
                positions = await self.broker.positions()
                open_orders = await self.broker.open_orders()
                if not positions and not open_orders:
                    applog.info("engine", "Emergency flatten successful: no active positions or open orders remaining.")
                    break

                if open_orders:
                    applog.warning(
                        "engine",
                        f"Emergency flatten: Cancelling {len(open_orders)} open orders (attempt {attempts+1})..."
                    )
                    await self.broker.cancel_all()

                if positions:
                    applog.warning(
                        "engine",
                        f"Emergency flatten: Flattening {len(positions)} active positions (attempt {attempts+1})..."
                    )
                    await self.broker.flatten_all(reason="fatal engine halt (retry)")

                await self._drain_fills(utcnow())
            except Exception as exc:
                applog.error("engine", f"Emergency flatten attempt {attempts+1} failed: {exc}")

            attempts += 1
            await asyncio.sleep(10.0)

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
                
                # Wait for fills to arrive and clear positions
                import asyncio
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
            if self.last_error.startswith("CRITICAL EOD liquidation unresolved"):
                self.last_error = ""
            applog.info("engine", "End-of-day liquidation authoritatively complete")
            # Explicitly clear out all PIS memory states for the next session
            self.pis_adapter.service.close_all()
            
            # Refresh contextual aggregate memory at EOD
            try:
                from app.strategies import refresh_contextual_memory
                refresh_contextual_memory(self.db)
            except Exception as exc:
                applog.error("engine", f"Failed contextual aggregates refresh: {exc}")
            
            # Compile EOD policy reflection report (CR-146 Phase 7: auto-promotion enabled)
            try:
                from app.strategies import generate_eod_reflection_report
                generate_eod_reflection_report(self.db, now=now, engine=self)
            except Exception as exc:
                applog.error("engine", f"Failed EOD reflection report generation: {exc}")
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

