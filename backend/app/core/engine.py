"""Small production trading engine.

Live path: data -> scanner -> strategy -> ML confirmation -> risk -> broker -> PIS.
Research, replay, training, portfolio optimization and telemetry never decide entries.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.brokers.base import Broker, make_broker
from app.brokers.paper import PaperBroker
from app.config import DATA_DIR, RiskConfig, Settings
from app.core.applog import applog
from app.core.clock import MarketClock
from app.core.domain import EngineState, OrderRequest, Position, Signal, SignalAction, utcnow
from app.core.entry_reservations import SameCycleEntryReservations
from app.data.manager import DataManager, make_provider
from app.db.database import Database
from app.indicators.ta import enrich
from app.pis import PISEngineAdapter, PISConfig
from app.risk.manager import AccountContext, RiskManager
from app.strategies import Strategy, create_strategies
from app.strategies.base import StrategyContext

ET = ZoneInfo("America/New_York")


class TradingEngine:
    """Single owner of the live trading path."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.mode = settings.mode
        self.clock = MarketClock()
        self.data = DataManager(make_provider(settings), db=db)
        self.broker: Broker = make_broker(settings, db=db) if settings.mode != "paper" else PaperBroker(db=db)
        self.risk = RiskManager(settings.risk, self.clock)
        self.strategies: list[Strategy] = create_strategies(settings)
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Any] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.watchlist: list[str] = []
        self.state = EngineState.IDLE
        self.last_error: str | None = None
        self.last_scan: datetime | None = None
        self.last_bar: datetime | None = None
        self.last_order: datetime | None = None
        self.last_strategy: datetime | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._entry_reservations = SameCycleEntryReservations()
        self.pis = PISEngineAdapter(PISConfig())
        self.publish = lambda *_args, **_kwargs: None

    async def ensure_data(self) -> None:
        await self._load_state()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self.state = EngineState.RUNNING
        self._task = asyncio.create_task(self._run(), name="trading-engine")
        applog.info("engine", "Simple trading engine started")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.state = EngineState.IDLE

    async def _run(self) -> None:
        interval = max(1.0, float(getattr(self.settings, "bar_interval_seconds", 60)))
        while not self._stop.is_set():
            started = utcnow()
            try:
                await self._cycle()
                self.last_bar = utcnow()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                applog.exception("engine", f"Trading cycle failed: {exc}")
            delay = max(0.1, interval - (utcnow() - started).total_seconds())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _cycle(self) -> None:
        now = utcnow()
        self._entry_reservations.clear()
        await self._sync_broker()
        if self.risk.should_flatten(now):
            await self._flatten()
            return
        if not self.watchlist:
            await self.scan()
        await self._evaluate_symbols(now)

    async def scan(self) -> list[str]:
        candidates = await self.data.scan() if hasattr(self.data, "scan") else []
        self.watchlist = [str(x).upper() for x in candidates][:100]
        self.last_scan = utcnow()
        return self.watchlist

    async def _evaluate_symbols(self, now: datetime) -> None:
        for symbol in list(self.watchlist):
            try:
                bars = await self.data.bars(symbol, timeframe="1Min", limit=240)
                if bars is None or bars.empty:
                    continue
                bars = enrich(bars)
                ctx = StrategyContext(now=now, mover_bias=None)
                for strategy in self.strategies:
                    if not getattr(strategy, "enabled", True):
                        continue
                    signal = strategy.on_bar(ctx, symbol, bars)
                    if signal is None:
                        continue
                    self.last_strategy = now
                    if not self._ml_ok(signal):
                        continue
                    account = await self._account_context()
                    decision = self.risk.evaluate_entry(signal, account, bars, now)
                    if not decision.approved or decision.order is None:
                        continue
                    if not self._entry_reservations.reserve(symbol):
                        continue
                    await self._submit(decision.order)
                    break
            except Exception as exc:
                applog.warning("engine", f"{symbol} evaluation failed: {exc}")

    def _ml_ok(self, signal: Signal) -> bool:
        """Optional single-model confirmation; absence of a model never blocks a strategy."""
        model = getattr(self, "signal_model", None)
        if model is None:
            return True
        try:
            probability = float(model.predict_signal(signal))
            threshold = float(getattr(self.settings.risk, "ml_confidence_threshold", 0.50))
            return probability >= threshold
        except Exception as exc:
            applog.warning("engine", f"ML confirmation unavailable; allowing strategy signal: {exc}")
            return True

    async def _submit(self, request: OrderRequest) -> None:
        order = await self.broker.submit(request)
        self.last_order = utcnow()
        self.orders[getattr(order, "id", request.symbol)] = order
        await self._persist_order(order)

    async def _sync_broker(self) -> None:
        try:
            positions = await self.broker.positions()
            self.positions = {p.symbol.upper(): p for p in positions}
        except Exception as exc:
            applog.warning("engine", f"Broker reconciliation failed: {exc}")

    async def _account_context(self) -> AccountContext:
        account = await self.broker.account()
        equity = float(account.equity)
        return AccountContext(
            equity=equity,
            cash=float(account.cash),
            positions=list(self.positions.values()),
            day_pnl=float(getattr(account, "day_pnl", 0.0) or 0.0),
            session_start_equity=equity - float(getattr(account, "day_pnl", 0.0) or 0.0),
            peak_equity=equity,
            day_trades_today=int(getattr(account, "day_trades_today", 0) or 0),
            rolling_day_trades=int(getattr(account, "day_trades_today", 0) or 0),
            is_paper=self.mode == "paper",
            pending_entry_symbols=list(self._entry_reservations.symbols()),
        )

    async def _flatten(self) -> None:
        for position in list(self.positions.values()):
            try:
                await self.broker.close_position(position.symbol)
            except Exception as exc:
                applog.error("engine", f"Failed to flatten {position.symbol}: {exc}")

    async def _load_state(self) -> None:
        try:
            await self._sync_broker()
        except Exception:
            pass

    async def _persist_order(self, order: Any) -> None:
        try:
            if hasattr(self.db, "save_order"):
                self.db.save_order(order)
        except Exception as exc:
            applog.warning("engine", f"Order persistence failed: {exc}")

    def status(self) -> dict:
        return {
            "state": getattr(self.state, "value", str(self.state)),
            "mode": self.mode,
            "watchlist": self.watchlist,
            "positions": len(self.positions),
            "last_scan": self.last_scan.isoformat() if self.last_scan else None,
            "last_bar": self.last_bar.isoformat() if self.last_bar else None,
            "last_order": self.last_order.isoformat() if self.last_order else None,
            "last_strategy": self.last_strategy.isoformat() if self.last_strategy else None,
            "last_error": self.last_error,
            "risk": self.risk.status(),
        }

    def quotes_payload(self) -> dict:
        return self.quotes

    def metrics_payload(self) -> dict:
        return {"positions": len(self.positions), "watchlist_size": len(self.watchlist)}

    @property
    def insights(self):
        class EmptyInsights:
            @staticmethod
            def recent(_limit=30):
                return []
        return EmptyInsights()
