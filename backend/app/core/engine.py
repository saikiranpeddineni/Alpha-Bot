"""TradingEngine -- the live orchestrator.

Async loop wiring every subsystem together, one tick at a time:

    data poll -> paper fill simulation / broker fill poll -> portfolio
    -> session roll -> kill switches -> EOD flatten
    -> strategies -> AI signal pipeline -> risk sizing -> broker
    -> equity marking -> UI broadcast

The identical strategy/risk/fill code paths run inside the backtester, so
behavior validated offline is the behavior that trades.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.ai.insights import InsightEngine
from app.ai.gemini_ranker import GeminiShadowRanker
from app.ai.shadow_bridge import GeminiShadowBridge
from app.ai.model import SignalModel
from app.ai.regime import detect_regime
from app.ai.trainer import ContinuousTrainer
from app.backtest.metrics import compute_metrics
from app.brokers.base import Broker, make_broker
from app.brokers.paper import PaperBroker
from app.config import DATA_DIR, MODELS_DIR, RiskConfig, Settings
from app.core.clock import MarketClock
from app.core.domain import (
    EngineState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Regime,
    SignalAction,
    utcnow,
)
from app.core.logging import applog
from app.core.entry_reservations import SameCycleEntryReservations
from app.data.manager import DataManager, make_provider
from app.db.database import Database
from app.indicators.ta import enrich, compute_relative_strength
from app.portfolio.tracker import PortfolioTracker
from app.risk.manager import AccountContext, RiskManager
from app.strategies import SignalPipeline, Strategy, create_strategies
from app.strategies.base import StrategyContext

from app.indicators.slot_volume import SlotVolumeEngine
from app.indicators.vwap_telemetry import compute_vwap_telemetry
from app.core.shadow_ledger import ShadowLedger
from app.core.counterfactual import ExitCounterfactualEngine

ET = ZoneInfo("America/New_York")

MAX_CONSECUTIVE_TICK_ERRORS = 30
EQUITY_PERSIST_EVERY_TICKS = 12
METRICS_BROADCAST_EVERY_TICKS = 6

# DB key template for durable kill-switch state.  Namespaced by mode so
# paper and live halt states are fully independent.
_HALT_KEY = "hard_halt_state:{mode}"


class TradingEngine:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.publish: Callable[[str, Any], None] = lambda _t, _d: None

        self.state: EngineState = EngineState.STOPPED
        self.mode: str = settings.mode
        self.last_error: str = ""

        self.model = SignalModel(MODELS_DIR)
        self.insights = InsightEngine(
            gemini_api_key=settings.gemini_api_key,
            anthropic_api_key=settings.anthropic_api_key,
        )
        self.insights.is_research_mode = False

        self.user_watchlist: list[str] = db.get_setting("watchlist") or settings.watchlist_symbols()
        self.scanned_watchlist: list[str] = []
        self.watchlist: list[str] = sorted(list(set(self.user_watchlist + self.scanned_watchlist)))
        self.core_watchlist: list[str] = list(self.user_watchlist)
        stored_risk = db.get_setting("risk_config") or {}
        self.risk_config = RiskConfig(**{**settings.risk_config().model_dump(), **stored_risk})

        # Built by start() / ensure_data()
        self.clock = MarketClock(always_open=(self.mode == "demo"))
        self.data: DataManager | None = None
        self.broker: Broker | None = None
        self.risk: RiskManager | None = None
        self.portfolio: PortfolioTracker | None = None
        self.pipeline: SignalPipeline | None = None
        self.strategies: list[Strategy] = create_strategies(db.get_setting("strategies"))

        self.recent_orders: deque[Order] = deque(maxlen=200)
        self.gemini_shadow = GeminiShadowBridge(
            GeminiShadowRanker(settings.gemini_api_key),
            observation_sink=self._persist_gemini_shadow_observation,
        )
        self._regimes: dict[str, Regime] = {}
        self._position_peaks: dict[str, float] = {}
        self._position_troughs: dict[str, float] = {}
        self._position_initial_stops: dict[str, float | None] = {}
        self._last_scan_date = None
        self._last_train_date = None
        self._last_event_monitor_min = None
        self._last_intraday_scan_min: int | None = None  # Feature 1: 30-min intraday refresh
        self._midday_active: bool = False                 # Feature 3: midday threshold state
        self._flattened_sessions: set = set()
        self._task: asyncio.Task | None = None
        self._tick_count = 0
        self._consec_errors = 0
        self._trades_since_insight = 0
        self._pending_close_symbols: set[str] = set()  # symbols with in-flight close order
        self.trainer: ContinuousTrainer | None = None

        # Feature 4: Economic calendar / event guard
        from app.core.calendar import EventCalendar
        self.calendar = EventCalendar(alpaca_client=None)  # client attached in ensure_data()

        # Performance Engine & Portfolio Manager
        from app.portfolio.performance import StrategyPerformanceTracker
        self.perf_tracker = StrategyPerformanceTracker()
        self.portfolio_gate = None
        self.portfolio_allocator = None

    # ------------------------------------------------------------------
    # kill-switch persistence helpers
    # ------------------------------------------------------------------

    def _halt_db_key(self, mode: str) -> str:
        return _HALT_KEY.format(mode=mode)

    def _persist_halt(self, mode: str, reason: str) -> None:
        """Write hard-halt state to the settings DB so it survives restarts."""
        self.db.set_setting(self._halt_db_key(mode), {"hard_halted": True, "reason": reason})

    def _clear_halt(self, mode: str) -> None:
        """Remove the persisted hard-halt record after an explicit operator reset."""
        self.db.set_setting(self._halt_db_key(mode), None)

    def _restore_halt_if_persisted(self, mode: str) -> bool:
        """Read DB and restore RiskManager halt state.  Returns True if halt was active."""
        if mode == "demo":
            # Demo is a local simulation; a persisted halt would only confuse normal use.
            return False
        record = self.db.get_setting(self._halt_db_key(mode))
        if record and record.get("hard_halted"):
            reason = record.get("reason", "persisted from previous session")
            if self.risk is not None:
                self.risk.hard_halted = True
                self.risk.hard_halt_reason = reason
            applog.error(
                "engine",
                f"KILL SWITCH RESTORED from DB (mode={mode}): {reason} "
                "-- engine will not start until operator calls reset_halt()",
            )
            return True
        return False

    # ------------------------------------------------------------------
    # data availability (charts work even while the engine is stopped)
    # ------------------------------------------------------------------

    async def ensure_data(self, mode: str | None = None) -> DataManager:
        mode = mode or self.mode
        provider_name = self.settings.resolve_data_provider(mode)
        if self.data is not None and self.data.provider.name == provider_name:
            return self.data
        if self.data is not None:
            await self.data.close()
        provider = make_provider(provider_name, self.settings)
        self.data = DataManager(provider, DATA_DIR / "bars_cache.db")
        applog.info("engine", f"Data provider ready: {provider_name}")
        try:
            await self.data.warmup(self.watchlist)
        except Exception as exc:
            applog.warning("engine", f"Failed to warmup watchlist on data initialization: {exc}")
        return self.data

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self, mode: str | None = None, confirm: str = "") -> None:
        if self.state == EngineState.RUNNING:
            raise RuntimeError("Engine is already running")
        if self.state == EngineState.HALTED:
            raise RuntimeError("Engine is halted by the risk kill switch -- reset the halt first")
        mode = (mode or self.mode).lower()
        if mode not in ("demo", "paper", "live"):
            raise ValueError(f"Unknown mode {mode!r}")

        if mode == "live":
            if not self.settings.enable_live_trading:
                raise PermissionError("Live trading is disabled. Set ENABLE_LIVE_TRADING=true in .env")
            if confirm != "LIVE":
                raise PermissionError("Live trading requires typed confirmation 'LIVE'")
            if not self.settings.has_alpaca_keys:
                raise PermissionError("Live trading requires Alpaca API keys in .env")

        self.mode = mode
        self.clock = MarketClock(always_open=(mode == "demo"))
        await self.ensure_data(mode)

        if mode == "demo":
            broker_name = "paper_sim"
        elif mode == "paper":
            broker_name = "alpaca" if (self.settings.broker == "alpaca" and self.settings.has_alpaca_keys) else "paper_sim"
        else:
            broker_name = "alpaca"
        if mode == "live" and broker_name != "alpaca":
            raise PermissionError("Live mode requires a real broker (alpaca)")

        settings = self.settings
        if mode == "live" and not settings.alpaca_paper:
            applog.warning("engine", "Starting in LIVE trading mode -- real money at risk")
        self.broker = make_broker(broker_name, settings)
        if isinstance(self.broker, PaperBroker) and mode == "live":
            raise PermissionError("Refusing to run live mode on the paper simulator")
        await self.broker.connect()

        self.risk = RiskManager(self.risk_config, self.clock)
        self.risk.core_watchlist = self.core_watchlist

        # --- #115 fix: restore durable kill-switch state before doing anything else.
        # If a hard halt was persisted from a previous session (crash, OOM, restart)
        # the operator must explicitly call reset_halt() before trading can resume.
        if self._restore_halt_if_persisted(mode):
            self.state = EngineState.HALTED
            self.publish("status", self.status())
            raise RuntimeError(
                f"Engine ({mode}): kill switch was active at last shutdown -- "
                "call reset_halt() to acknowledge and re-enable trading"
            )

        self.portfolio = PortfolioTracker()

        from app.portfolio.allocator import PortfolioAIGate, CapitalAllocationOptimizer
        self.portfolio_gate = PortfolioAIGate(
            max_correlation=self.risk_config.max_correlation_penalty,
            max_positions_per_sector=self.risk_config.max_positions_per_sector
        )
        self.portfolio_allocator = CapitalAllocationOptimizer(
            kelly_fraction_cap=self.risk_config.kelly_fraction_cap
        )

        # Feature 4: Attach Alpaca client to EventCalendar for live news checking
        try:
            from app.brokers.alpaca import AlpacaBroker
            if isinstance(self.broker, AlpacaBroker) and hasattr(self.broker, "_client"):
                self.calendar._alpaca = self.broker._client
        except Exception:
            pass  # Non-Alpaca broker or no client — calendar degrades gracefully

        # Restore strategy mapping & reconstruct portfolio lots for active positions from database
        try:
            positions = await self.broker.positions()
            for p in positions:
                strat = "manual"
                if hasattr(self.broker, "_symbol_strategies"):
                    db_strat = self.db.get_latest_order_strategy(p.symbol, self.mode)
                    if db_strat:
                        self.broker._symbol_strategies[p.symbol] = db_strat
                        strat = db_strat
                        applog.info("engine", f"Restored strategy '{db_strat}' for active position {p.symbol}")

                # Fetch full latest order details to populate stop loss and initial risk units
                order_payload = self.db.get_latest_order_payload(p.symbol, self.mode)
                stop_loss = None
                initial_risk = None
                opened_at = p.opened_at if hasattr(p, "opened_at") and p.opened_at else utcnow()
                if order_payload:
                    stop_loss = order_payload.get("stop_loss")
                    if stop_loss:
                        initial_risk = abs(p.avg_entry_price - stop_loss)
                    submitted_at_str = order_payload.get("submitted_at")
                    if submitted_at_str:
                        try:
                            opened_at = datetime.fromisoformat(submitted_at_str)
                        except Exception:
                            pass

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
        except Exception as exc:
            applog.error("engine", f"Failed to restore and reconcile active positions: {exc}")
        self.pipeline = SignalPipeline(self.model, self.risk_config.ml_confidence_threshold)
        for s in self.strategies:
            s.reset()
        self._regimes.clear()
        self._flattened_sessions.clear()
        self._last_scan_date = None
        self._tick_count = 0
        self._consec_errors = 0
        self.last_error = ""

        await self.data.warmup(self.watchlist)
        if isinstance(self.broker, PaperBroker):
            now = utcnow()
            for symbol, price in self.data.last_prices(self.watchlist).items():
                self.broker.update_price(symbol, price, now)

        account = await self.broker.account()
        self.portfolio.roll_session(self.clock.session_date(), account.equity)
        self.portfolio.peak_equity = account.equity
        self.portfolio.mark_equity(account.equity, utcnow())

        self.trainer = ContinuousTrainer(
            self.data, self.model, MODELS_DIR, self.db.get_setting("strategies") or {},
        )
        self._last_train_date = None
        self.state = EngineState.RUNNING
        self._task = asyncio.create_task(self._run_loop(), name="trading-engine")
        applog.info(
            "engine",
            f"Engine started -- mode={mode}, broker={self.broker.name}, "
            f"data={self.data.provider.name}, watchlist={','.join(self.watchlist)}",
        )
        self.insights.on_model_status(self.model.info())
        self.publish("status", self.status())

    async def stop(self, flatten: bool | None = None) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.broker is not None and flatten is None:
            flatten = isinstance(self.broker, PaperBroker)  # clean slate for the sim
        if self.broker is not None and flatten:
            try:
                await self.broker.flatten_all(reason="engine stop")
                await self._drain_fills(utcnow())
            except Exception as exc:  # noqa: BLE001
                applog.error("engine", f"flatten on stop failed: {exc}")
        if self.state != EngineState.HALTED:
            self.state = EngineState.STOPPED
        applog.info("engine", "Engine stopped")
        self.publish("status", self.status())

    async def _run_loop(self) -> None:
        interval = self.settings.demo_bar_seconds if self.mode == "demo" else self.settings.poll_interval_sec
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self._tick()
                self._consec_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._consec_errors += 1
                self.last_error = str(exc)
                applog.error("engine", f"tick failed ({self._consec_errors}): {exc}")
                if self._consec_errors >= MAX_CONSECUTIVE_TICK_ERRORS:
                    applog.error(
                        "engine",
                        f"FATAL: {self._consec_errors} consecutive errors reached. "
                        f"Halting engine and triggering emergency flatten."
                    )
                    self.state = EngineState.HALTED
                    self.publish("status", self.status())
                    # Spawn emergency flattening task
                    asyncio.create_task(
                        self._emergency_flatten_loop(),
                        name="trading-engine-emergency-flatten"
                    )
                    return
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.25, interval - elapsed))

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

    # ------------------------------------------------------------------
    # authoritative hard-halt liquidation
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # authoritative EOD liquidation
    # ------------------------------------------------------------------

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
                await self.broker.flatten_all(reason="end of day")
                if hasattr(self, "_pending_close_symbols"):
                    self._pending_close_symbols.clear()
                await self._drain_fills(now)
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
            self.publish("status", self.status())
            return True
        except BaseException as exc:
            self.last_error = f"CRITICAL EOD liquidation unresolved: {type(exc).__name__}: {exc}"
            applog.error("engine", self.last_error)
            self.publish("status", self.status())
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):\
                raise
            return False

    # ------------------------------------------------------------------
    # the tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        assert self.data and self.broker and self.risk and self.portfolio and self.pipeline
        self._tick_count += 1
        now = utcnow()

        # Run pre-market scanner (9:00 AM ET onwards) once per day
        # Run post-market trainer (>= 16:15 ET) once per day
        # Run event monitor every 5 minutes during active trading session (9:30 AM to 4:00 PM ET)
        if self.state == EngineState.RUNNING:
            from zoneinfo import ZoneInfo
            now_et = now.astimezone(ZoneInfo("America/New_York"))
            today_date = now_et.date()
            et_mins = now_et.hour * 60 + now_et.minute
            if self._last_scan_date != today_date and et_mins >= 9 * 60:
                self._last_scan_date = today_date
                applog.info("engine", "Pre-market scanner trigger time reached (>= 09:00 ET)")
                await self.run_market_scanner(is_intraday=False)
            # Feature 1: Intraday watchlist refresh every 30 min during session
            elif (
                9 * 60 + 30 <= et_mins <= 15 * 60 + 30  # 9:30 AM – 3:30 PM ET
                and et_mins % 30 == 0
                and self._last_intraday_scan_min != et_mins
            ):
                self._last_intraday_scan_min = et_mins
                applog.info("engine", f"Intraday watchlist refresh triggered at {et_mins} ET mins")
                await self.run_market_scanner(is_intraday=True)
            if self.trainer and self._last_train_date != today_date and et_mins >= 16 * 60 + 15:
                self._last_train_date = today_date
                applog.info("engine", "Post-market trainer trigger time reached (>= 16:15 ET)")
                await self.trainer.trigger(self.watchlist)
            # Feature 3: Midday score threshold boost (raise threshold, not hard block)
            rc = self.risk_config
            is_midday = rc.midday_start_et <= et_mins <= rc.midday_end_et
            if is_midday != self._midday_active:
                self._midday_active = is_midday
                if self.pipeline:
                    self.pipeline.set_midday_active(is_midday)
                applog.info(
                    "engine",
                    f"Midday filter {'ACTIVE — threshold +' + str(rc.midday_score_boost) if is_midday else 'CLEARED'} "
                    f"(et_mins={et_mins}, window={rc.midday_start_et}–{rc.midday_end_et})",
                )
            if 9 * 60 + 30 <= et_mins <= 16 * 60 and et_mins % 5 == 0 and self._last_event_monitor_min != et_mins:
                self._last_event_monitor_min = et_mins
                await self.run_event_monitor(now)

        new_closed = await self.data.poll(self.watchlist)

        # 1. advance fill simulation / poll real fills
        if isinstance(self.broker, PaperBroker):
            for symbol, bars in new_closed.items():
                for ts, row in bars.iterrows():
                    self.broker.process_bar(symbol, row, (ts + timedelta(minutes=1)).to_pydatetime())
        elif hasattr(self.broker, "poll_fills"):
            await self.broker.poll_fills()
        await self._drain_fills(now)

        account = await self.broker.account()
        positions = await self.broker.positions()

        # 2. session roll
        session = self.clock.session_date(now)
        if self.portfolio.roll_session(session, account.equity):
            self.risk.roll_session(session)
            self.insights.on_performance(self.portfolio.stats(), 0.0)

        # 3. kill switches
        ctx = AccountContext(
            equity=account.equity,
            cash=account.cash,
            positions=positions,
            day_pnl=self.portfolio.day_pnl(account.equity),
            session_start_equity=self.portfolio.session_start_equity or account.equity,
            peak_equity=self.portfolio.peak_equity,
            day_trades_today=self.portfolio.day_trades_today,
            rolling_day_trades=self.portfolio.day_trades_in_window(now),
            is_paper=self.broker.is_paper,
        )
        halt = self.risk.check_halts(ctx, now=now)
        if halt == "hard":
            # Latch the capital-preservation state before any fallible broker I/O.
            self.state = EngineState.HALTED
            # --- #115 fix: persist the halt immediately so a subsequent crash/restart
            # cannot silently re-enable trading without operator acknowledgement.
            self._persist_halt(self.mode, self.risk.hard_halt_reason)
            self.insights.on_risk_event(
                "Kill switch tripped", self.risk.hard_halt_reason, severity="danger"
            )
            await self._reconcile_hard_halt_liquidation(now)
            self.publish("status", self.status())
        elif self.state == EngineState.HALTED and self.risk.hard_halted:
            await self._reconcile_hard_halt_liquidation(now)
        elif halt == "daily":
            await self.broker.flatten_all(reason="daily loss halt")
            await self._drain_fills(now)
            self.insights.on_risk_event(
                "Daily loss limit hit",
                f"Trading paused until the next session (day P&L {ctx.day_pnl:+,.2f}).",
                severity="danger",
            )

        # 4. end-of-day flatten. Completion is latched only after an
        # authoritative broker post-condition proves no positions or open orders.
        in_flatten_window = self.risk.should_flatten(now)
        if (
            self.state == EngineState.RUNNING
            and in_flatten_window
            and session not in self._flattened_sessions
        ):
            await self._reconcile_eod_liquidation(session, now)

        # 5. strategy evaluation on newly closed bars. Do not reopen exposure
        # while EOD liquidation is active, including unresolved retry ticks.
        can_trade = (
            self.state == EngineState.RUNNING
            and not self.risk.daily_halted
            and not in_flatten_window
            and (self.clock.is_open(now) or self.mode == "demo")
        )
        if can_trade and new_closed:
            pos_by_symbol = {p.symbol: p for p in positions}
            candidates = []
            for symbol in new_closed:
                entries = await self._evaluate_symbol(symbol, pos_by_symbol.get(symbol), account.equity, now)
                candidates.extend(entries)

            if candidates:
                from app.portfolio.allocator import process_portfolio_allocations
                from app.risk.manager import is_entry_order

                # Gather candidate signals
                alloc_signals = [sig for sig, _ in candidates]
                df_by_symbol = {sig.symbol: df for sig, df in candidates}

                # Shared portfolio gate and allocation
                decision_outcome = process_portfolio_allocations(
                    signals=alloc_signals,
                    positions=positions,
                    available_capital=account.cash,
                    equity=account.equity,
                    portfolio_gate=self.portfolio_gate,
                    portfolio_allocator=self.portfolio_allocator,
                )

                # Shadow research observes only deterministic post-allocation candidates.
                # The bridge has no return channel into risk, order construction, or submission.
                self.gemini_shadow.schedule(
                    [sig for sig, _ in decision_outcome.approved_entries],
                    session=session,
                )

                # Process gated/rejected entries
                for sig, reason in decision_outcome.gated_entries:
                    applog.info("engine", f"{sig.symbol}: Portfolio Gate/Allocator rejected -- {reason}")
                    self.publish("insight", self.insights.on_signal(sig, gated=True, gate_reason=reason).model_dump(mode="json"))

                # Size and submit approved entries
                if decision_outcome.approved_entries:
                    open_orders = await self.broker.open_orders()
                    pending_entries = [
                        o.symbol.upper() for o in open_orders
                        if is_entry_order(o, positions)
                    ]
                    effective_pending_entries = set(pending_entries)
                    reservations = SameCycleEntryReservations.from_snapshot(account.cash, positions)

                    for sig, kelly_val in decision_outcome.approved_entries:
                        df = df_by_symbol[sig.symbol]
                        ctx = AccountContext(
                            equity=account.equity,
                            cash=reservations.cash,
                            positions=reservations.positions,
                            day_pnl=self.portfolio.day_pnl(account.equity),
                            session_start_equity=self.portfolio.session_start_equity or account.equity,
                            peak_equity=self.portfolio.peak_equity,
                            day_trades_today=self.portfolio.day_trades_today,
                            rolling_day_trades=self.portfolio.day_trades_in_window(now),
                            is_paper=self.broker.is_paper,
                            pending_entry_symbols=list(effective_pending_entries),
                        )
                        decision = self.risk.evaluate_entry(sig, ctx, df, now=now, kelly_override=kelly_val)
                        if not decision.approved:
                            applog.info("engine", f"{sig.symbol.upper()}: entry rejected by RiskManager — {decision.reason}")
                            self.publish("insight", self.insights.on_signal(sig, gated=True, gate_reason=decision.reason).model_dump(mode="json"))
                            continue
                        order = await self.broker.submit(decision.order)
                        self.recent_orders.appendleft(order)
                        self.db.save_order(order, self.mode)
                        self.publish("insight", self.insights.on_signal(sig, gated=False).model_dump(mode="json"))
                        self.publish("orders", self.orders_payload())
                        
                        # Reserve active same-cycle submissions before evaluating the next candidate.
                        # This overlays the stale broker snapshot for cash, position-count, and sector checks.
                        reference_price = sig.price or float(df["close"].iloc[-1])
                        if reservations.reserve(order, reference_price):
                            effective_pending_entries.add(sig.symbol.upper())

            await self._drain_fills(now)

        # 6. mark equity + persist
        account = await self.broker.account() if new_closed else account
        self.portfolio.mark_equity(account.equity, now)
        if self._tick_count % EQUITY_PERSIST_EVERY_TICKS == 0:
            self.db.save_equity(account.equity, account.cash, self.mode, now)

        # 7. broadcast
        self._broadcast_tick(account, now, new_closed, positions)

    async def _evaluate_symbol(self, symbol: str, position, equity: float, now: datetime) -> list[tuple[Signal, pd.DataFrame]]:  # noqa: ANN001
        assert self.data and self.broker and self.risk and self.pipeline
        df = enrich(self.data.frame(symbol, "1Min", limit=600))
        if df.empty or len(df) < 30:
            return []

        regime, details = detect_regime(df)
        if self._regimes.get(symbol) != regime:
            self._regimes[symbol] = regime
            self.publish("insight", self.insights.on_regime_change(symbol, regime, details).model_dump(mode="json"))

        # Trailing stop update & check
        if position is not None:
            close = float(df["close"].iloc[-1])
            high_val = float(df["high"].iloc[-1])
            low_val = float(df["low"].iloc[-1])
            if symbol not in self._position_peaks:
                self._position_peaks[symbol] = position.avg_entry_price
                self._position_troughs[symbol] = position.avg_entry_price
                self._position_initial_stops[symbol] = position.stop_loss
            
            if position.qty > 0:
                self._position_peaks[symbol] = max(self._position_peaks[symbol], high_val)
                self._position_troughs[symbol] = min(self._position_troughs[symbol], low_val)
            else:
                self._position_peaks[symbol] = min(self._position_peaks[symbol], low_val)
                self._position_troughs[symbol] = max(self._position_troughs[symbol], high_val)

            strat = next((s for s in self.strategies if s.name == position.strategy), None)
            if strat is not None:
                ts_mult = strat.params.get("trailing_stop_atr_mult")
                if ts_mult is not None and ts_mult > 0:
                    atr = float(df["atr14"].iloc[-1]) if pd.notna(df.get("atr14").iloc[-1]) else 0.0
                    if atr > 0:
                        if position.qty > 0:
                            stop_price = self._position_peaks[symbol] - ts_mult * atr
                            if close <= stop_price:
                                applog.info("engine", f"{symbol}: trailing stop triggered @ {close:.2f} (stop {stop_price:.2f})")
                                await self._close_position_guarded(symbol, reason=f"trailing stop triggered ({ts_mult}x ATR)")
                                if symbol in self._position_peaks:
                                    del self._position_peaks[symbol]
                                if symbol in self._position_troughs:
                                    del self._position_troughs[symbol]
                                if symbol in self._position_initial_stops:
                                    del self._position_initial_stops[symbol]
                                return []
                        else:
                            stop_price = self._position_peaks[symbol] + ts_mult * atr
                            if close >= stop_price:
                                applog.info("engine", f"{symbol}: trailing stop triggered @ {close:.2f} (stop {stop_price:.2f})")
                                await self._close_position_guarded(symbol, reason=f"trailing stop triggered ({ts_mult}x ATR)")
                                if symbol in self._position_peaks:
                                    del self._position_peaks[symbol]
                                if symbol in self._position_troughs:
                                    del self._position_troughs[symbol]
                                if symbol in self._position_initial_stops:
                                    del self._position_initial_stops[symbol]
                                return []
        else:
            if symbol in self._position_peaks:
                del self._position_peaks[symbol]
            if symbol in self._position_troughs:
                del self._position_troughs[symbol]
            if symbol in self._position_initial_stops:
                del self._position_initial_stops[symbol]

        sctx = StrategyContext(position=position, equity=equity, regime=regime, now=now)
        signals = []
        for strat in self.strategies:
            if not strat.enabled:
                continue
            try:
                sig = strat.on_bar(sctx, symbol, df)
            except Exception as exc:  # noqa: BLE001
                applog.error("engine", f"strategy {strat.name} failed on {symbol}: {exc}")
                continue
            if sig is not None:
                signals.append(sig)
        if not signals:
            return []

        # Feature 2: Relative Strength vs SPY — multi-timeframe (40% ret_5 + 60% ret_15)
        spy_df = None
        rs_score: float | None = None
        try:
            spy_df = enrich(self.data.frame("SPY", "1Min", limit=600))
            rs_score = compute_relative_strength(df, spy_df)
            if rs_score is not None:
                applog.debug("engine", f"{symbol} RS vs SPY: rs_score={rs_score:.3f}")
        except Exception:
            rs_score = None  # graceful fallback — pipeline treats None as neutral 50

        accepted, rejected = self.pipeline.process(symbol, df, signals, regime, sector_strength=rs_score)
        
        # Save signals to shadow ledger
        for sig in signals:
            if sig.action == SignalAction.EXIT:
                continue
            is_accepted = sig in accepted
            final_decision = "accepted" if is_accepted else "rejected"
            rejection_stage = "none"
            rejection_reason = ""
            if not is_accepted:
                rejection_reason = "rejected"
                for rsig, rreason in rejected:
                    if rsig.strategy == sig.strategy and rsig.action == sig.action:
                        rejection_reason = rreason
                        break
                if "ML confidence" in rejection_reason:
                    rejection_stage = "ml_gate"
                elif "Trade Score" in rejection_reason:
                    rejection_stage = "trade_score_gate"
                elif "lost conflict" in rejection_reason:
                    rejection_stage = "conflict_resolution"
                else:
                    rejection_stage = "other"

            try:
                slot_engine = SlotVolumeEngine(df, lookback_sessions=20, min_observations=5)
                slot_rvol_val = slot_engine.get_slot_rvol(now)
            except Exception:
                slot_rvol_val = None

            try:
                vwap_tel = compute_vwap_telemetry(df, now, slot_rvol_val)
            except Exception:
                vwap_tel = {}

            ShadowLedger.record_candidate(
                db_engine=self.db,
                symbol=symbol,
                ts=now,
                strategy=sig.strategy,
                direction="long" if sig.action == SignalAction.ENTER_LONG else "short",
                signal_price=sig.price or float(df["close"].iloc[-1]),
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                ml_confidence=sig.ml_probability,
                final_decision=final_decision,
                rejection_stage=rejection_stage,
                rejection_reason=rejection_reason,
                df=df,
                spy_df=spy_df if spy_df is not None else pd.DataFrame(),
                slot_rvol_val=slot_rvol_val,
                vwap_telemetry=vwap_tel,
                mode=self.mode,
            )

        for sig, reason in rejected:
            applog.debug("pipeline", f"{symbol} {sig.strategy} {sig.action}: rejected -- {reason}")

        candidate_entries = []
        for sig in accepted:
            if sig.action == SignalAction.EXIT:
                if position is not None:
                    applog.info("engine", f"{symbol}: strategy exit -- {sig.reason}")
                    await self._close_position_guarded(symbol, reason=sig.reason)
                continue
            
            # Record initial stop-loss when entries are accepted
            self._position_initial_stops[symbol] = sig.stop_loss
            candidate_entries.append((sig, df))
        return candidate_entries

    async def _close_position_guarded(self, symbol: str, reason: str) -> None:
        """Close a position, skipping if a close order is already in-flight for this symbol."""
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        if symbol in self._pending_close_symbols:
            applog.debug("engine", f"{symbol}: skipping duplicate close — close already in-flight")
            return
        self._pending_close_symbols.add(symbol)
        order = await self.broker.close_position(symbol, reason=reason)
        # If close failed immediately (None returned), release the guard so retries work
        if order is None:
            self._pending_close_symbols.discard(symbol)

    async def _drain_fills(self, now: datetime) -> None:
        assert self.broker and self.portfolio and self.risk
        fills = self.broker.drain_fills()
        if not fills:
            return
        order_by_id = {o.id: o for o in self.recent_orders}
        for fill in fills:
            order = order_by_id.get(fill.order_id)
            if order is not None and order.status != OrderStatus.FILLED:
                # Paper orders share objects and are already updated; Alpaca
                # orders are snapshots that need their fill state applied.
                order.status = OrderStatus.FILLED
                order.filled_qty = fill.qty
                order.filled_avg_price = fill.price
                order.filled_at = fill.ts
            
            symbol = fill.symbol.upper()
            # Release pending-close guard when sell fill arrives
            if fill.side == OrderSide.SELL and hasattr(self, "_pending_close_symbols"):
                self._pending_close_symbols.discard(symbol)
            peak = self._position_peaks.get(symbol)
            trough = self._position_troughs.get(symbol)
            init_stop = self._position_initial_stops.get(symbol)

            trade = self.portfolio.on_fill(fill, peak_price=peak, trough_price=trough, initial_stop=init_stop)
            if trade is not None:
                self.risk.on_trade_closed(trade, now=now)
                self.perf_tracker.update(trade, now=now)
                self.db.save_trade(trade, self.mode)
                self.publish("trade", trade.model_dump(mode="json"))

                # Exit counterfactuals replay on live completed trade
                try:
                    sym_frame = enrich(self.data.frame(symbol, "1Min", limit=600)) if self.data else pd.DataFrame()
                    if not sym_frame.empty:
                        entry_idx = sym_frame.index.get_loc(trade.entry_at)
                        entry_price = trade.entry_price
                        side = trade.side
                        init_stop_price = init_stop if init_stop is not None else entry_price * (0.985 if side == "long" else 1.015)
                        entry_bar = sym_frame.iloc[entry_idx]
                        initial_atr = float(entry_bar["atr14"]) if "atr14" in entry_bar and pd.notna(entry_bar["atr14"]) else (entry_price * 0.01)
                        
                        counterfactuals = ExitCounterfactualEngine.replay_all_policies(
                            df=sym_frame,
                            entry_idx=entry_idx,
                            entry_price=entry_price,
                            side=side,
                            initial_stop=init_stop_price,
                            initial_atr=initial_atr,
                        )
                        applog.info("counterfactual", f"Live Closed Trade {symbol} Exit Counterfactual Replay: {counterfactuals}")
                except Exception:
                    pass

                # Cleanup peak/trough/stop
                if symbol in self._position_peaks:
                    del self._position_peaks[symbol]
                if symbol in self._position_troughs:
                    del self._position_troughs[symbol]
                if symbol in self._position_initial_stops:
                    del self._position_initial_stops[symbol]

                self._trades_since_insight += 1
                if self._trades_since_insight >= 5:
                    self._trades_since_insight = 0
                    day_pnl = self.portfolio.day_pnl((await self.broker.account()).equity)
                    self.publish(
                        "insight",
                        self.insights.on_performance(self.portfolio.stats(), day_pnl).model_dump(mode="json"),
                    )
            else:
                stop = None
                if order is not None and order.stop_loss is not None:
                    stop = order.stop_loss
                elif isinstance(self.broker, PaperBroker):
                    pos = self.broker._positions.get(fill.symbol)
                    stop = pos.stop_loss if pos is not None else None
                self.portfolio.note_stop(fill.symbol, stop)
        self.publish("orders", self.orders_payload())

    # ------------------------------------------------------------------
    # broadcast payloads
    # ------------------------------------------------------------------

    def _broadcast_tick(self, account, now: datetime, new_closed: dict, positions: list) -> None:  # noqa: ANN001
        assert self.portfolio is not None
        self.publish("account", {
            "equity": account.equity,
            "cash": account.cash,
            "buying_power": account.buying_power,
            "day_pnl": round(self.portfolio.day_pnl(account.equity), 2),
            "day_pnl_pct": round(
                self.portfolio.day_pnl(account.equity)
                / (self.portfolio.session_start_equity or account.equity or 1.0) * 100.0, 3,
            ),
            "drawdown_pct": round(self.portfolio.drawdown_pct(account.equity), 2),
        })
        self.publish("quotes", self.quotes_payload())
        self.publish("positions", [p.to_payload() for p in positions])
        for symbol, bars in new_closed.items():
            last_ts = bars.index[-1]
            row = bars.iloc[-1]
            self.publish("bar", {
                "symbol": symbol,
                "bar": {
                    "t": int(last_ts.timestamp()),
                    "o": round(float(row["open"]), 4),
                    "h": round(float(row["high"]), 4),
                    "l": round(float(row["low"]), 4),
                    "c": round(float(row["close"]), 4),
                    "v": float(row["volume"]),
                },
            })
        if self._tick_count % METRICS_BROADCAST_EVERY_TICKS == 0:
            self.publish("metrics", self.metrics_payload())
        self.publish("status", self.status())

    async def _publish_positions(self) -> None:
        if self.broker is None:
            return
        try:
            positions = await self.broker.positions()
        except Exception:  # noqa: BLE001
            return
        self.publish("positions", [p.to_payload() for p in positions])

    def quotes_payload(self) -> list[dict]:
        if self.data is None:
            return []
        out = []
        for symbol in self.watchlist:
            df = self.data.frame(symbol, "1Min")
            if df.empty:
                continue
            session = df.index.tz_convert(ET).date
            today = session == session[-1]
            day_open = float(df.loc[today, "open"].iloc[0])
            price = float(df["close"].iloc[-1])
            out.append({
                "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round((price / day_open - 1.0) * 100.0, 2) if day_open else 0.0,
                "volume": float(df.loc[today, "volume"].sum()),
                "regime": self._regimes.get(symbol),
            })
        return out

    def orders_payload(self) -> list[dict]:
        return [o.model_dump(mode="json") for o in list(self.recent_orders)[:60]]

    def metrics_payload(self) -> dict:
        if self.portfolio is None:
            return {"stats": {}, "metrics": {}, "equity_curve": [], "performance": {}}
        curve = self.portfolio.equity_curve[-600:]
        start_eq = self.portfolio.equity_curve[0][1] if self.portfolio.equity_curve else 0.0
        return {
            "stats": self.portfolio.stats(),
            "metrics": compute_metrics(self.portfolio.equity_curve, self.portfolio.trades, start_eq or 1.0),
            "equity_curve": [[ts.isoformat(), round(eq, 2)] for ts, eq in curve],
            "performance": self.perf_tracker.summary(),
        }

    def status(self) -> dict:
        return {
            "state": self.state,
            "mode": self.mode,
            "provider": self.data.provider.name if self.data else self.settings.resolve_data_provider(),
            "broker": self.broker.name if self.broker else None,
            "market": self.clock.status(),
            "watchlist": self.watchlist,
            "core_watchlist": self.user_watchlist,
            "risk": self.risk.status() if self.risk else {"config": self.risk_config.model_dump()},
            "model": self.model.info(),
            "trainer": self.trainer.status if self.trainer else {"training_in_progress": False, "last_trained": None, "last_result": {}},
            "live_trading_enabled": self.settings.enable_live_trading,
            "has_alpaca_keys": self.settings.has_alpaca_keys,
            "gemini_shadow": self.gemini_shadow.ranker.telemetry(),
            "tick": self._tick_count,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------
    # manual operations (REST)
    # ------------------------------------------------------------------

    async def manual_order(self, req: OrderRequest) -> Order:
        if self.state != EngineState.RUNNING or self.broker is None:
            raise RuntimeError("Engine is not running")
        if self.risk and self.risk.hard_halted:
            raise RuntimeError("Hard halt active -- reset the kill switch first")

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
        order = await self.broker.submit(req)
        self.recent_orders.appendleft(order)
        self.db.save_order(order, self.mode)
        await self._drain_fills(utcnow())
        self.publish("orders", self.orders_payload())
        await self._publish_positions()
        return order

    async def close_symbol(self, symbol: str) -> Order | None:
        if self.broker is None:
            raise RuntimeError("Engine is not running")
        order = await self.broker.close_position(symbol, reason="manual close")
        if order is not None:
            self.recent_orders.appendleft(order)
            self.db.save_order(order, self.mode)
        await self._drain_fills(utcnow())
        await self._publish_positions()
        return order

    async def flatten(self) -> None:
        if self.broker is None:
            raise RuntimeError("Engine is not running")
        await self.broker.flatten_all(reason="manual flatten")
        await self._drain_fills(utcnow())
        await self._publish_positions()

    def reset_halt(self) -> None:
        if self.risk is not None:
            self.risk.reset_hard_halt()
        # --- #115 fix: clear the durable DB record so the next start() proceeds normally.
        self._clear_halt(self.mode)
        if self.state == EngineState.HALTED:
            self.state = EngineState.STOPPED
        self.publish("status", self.status())

    async def set_watchlist(self, symbols: list[str]) -> list[str]:
        cleaned = sorted({s.strip().upper() for s in symbols if s.strip()})
        if not cleaned:
            raise ValueError("Watchlist cannot be empty")
        if len(cleaned) > 30:
            raise ValueError("Watchlist is capped at 30 symbols")

        current_active = set(self.watchlist)
        new_active = set(cleaned)

        additions = new_active - current_active
        for sym in additions:
            if sym not in self.user_watchlist:
                self.user_watchlist.append(sym)

        removals = current_active - new_active
        for sym in removals:
            if sym in self.user_watchlist:
                self.user_watchlist.remove(sym)
            if sym in self.scanned_watchlist:
                self.scanned_watchlist.remove(sym)

        self.user_watchlist = sorted(list(set(self.user_watchlist)))
        self.scanned_watchlist = sorted(list(set(self.scanned_watchlist)))
        self.watchlist = sorted(list(set(self.user_watchlist + self.scanned_watchlist)))
        self.core_watchlist = list(self.user_watchlist)

        self.db.set_setting("watchlist", self.user_watchlist)

        if self.data is not None and additions:
            await self.data.warmup(list(additions))

        self.publish("status", self.status())
        self.publish("quotes", self.quotes_payload())
        return self.watchlist

    def update_risk_config(self, partial: dict) -> RiskConfig:
        merged = {**self.risk_config.model_dump(), **partial}
        self.risk_config = RiskConfig(**merged)  # validates bounds
        self.db.set_setting("risk_config", self.risk_config.model_dump())
        if self.risk is not None:
            self.risk.config = self.risk_config
        if self.pipeline is not None:
            self.pipeline.ml_threshold = self.risk_config.ml_confidence_threshold
            if self.pipeline.trade_score_engine is not None:
                self.pipeline.trade_score_engine.risk_config = self.risk_config
        applog.info("risk", f"Risk config updated: {partial}")
        self.publish("status", self.status())
        return self.risk_config

    def update_strategy(self, name: str, enabled: bool | None = None, params: dict | None = None) -> dict:
        strat = next((s for s in self.strategies if s.name == name), None)
        if strat is None:
            raise ValueError(f"Unknown strategy {name!r}")
        if enabled is not None:
            strat.enabled = enabled
        if params:
            strat.params.update(params)
        overrides = self.db.get_setting("strategies") or {}
        overrides[name] = {"enabled": strat.enabled, "params": strat.params}
        self.db.set_setting("strategies", overrides)
        if self.trainer is not None:
            self.trainer.strategy_overrides = overrides
        applog.info("engine", f"Strategy {name}: enabled={strat.enabled}")
        return strat.describe()

    async def train_model(self, symbols: list[str] | None, days: int = 30) -> dict:
        data = await self.ensure_data()
        symbols = [s.upper() for s in (symbols or self.watchlist)]
        end = utcnow()
        start = end - timedelta(days=days)
        frames = {}
        for symbol in symbols:
            df = await data.history(symbol, "1Min", start, end)
            if not df.empty:
                frames[symbol] = df
        if not frames:
            raise ValueError("No training data available for the requested symbols")
        applog.info("ai.model", f"Training on {len(frames)} symbols, {days} days of 1m bars...")
        info = await asyncio.to_thread(
            self.model.train, frames, self.settings.ml_horizon_bars, 0.10
        )
        self.publish("insight", self.insights.on_model_status(info).model_dump(mode="json"))
        self.publish("status", self.status())
        return info

    async def run_market_scanner(self, is_intraday: bool = False) -> list[str]:
        from app.core.scanner import MarketScanner, ScannerOutcome
        scanner = MarketScanner(self.data, self.user_watchlist, risk_config=self.risk_config)
        scan_label = "Intraday refresh" if is_intraday else "Pre-market"
        applog.info("engine", f"Starting autonomous market scanner ({scan_label} scan)...")
        outcome, new_watchlist = await scanner.run()
        if outcome == ScannerOutcome.FAILED:
            applog.error("engine", "Market scanner failed; preserving the existing watchlist")
            return []
        if outcome == ScannerOutcome.SUCCESS_EMPTY:
            applog.info("engine", "Market scanner returned no eligible symbols; preserving the existing watchlist")
            return []
        if new_watchlist:
            # Feature 1: Hysteresis — protect symbols with open positions from removal
            open_symbols: set[str] = set()
            try:
                if self.broker:
                    positions = await self.broker.positions()
                    open_symbols = {p.symbol for p in positions}
            except Exception:
                pass

            if is_intraday:
                # Intraday: only add new symbols; retain all existing except those
                # that dropped off scanner AND have no open position
                existing = set(self.scanned_watchlist)
                new_set = set(new_watchlist)
                # Symbols to remove: in existing but not in new scan, no open position
                to_remove = (existing - new_set) - open_symbols
                # Symbols to add: in new scan but not yet in existing
                to_add = new_set - existing
                updated = (existing - to_remove) | to_add
                self.scanned_watchlist = sorted(updated)
                if to_add or to_remove:
                    applog.info(
                        "engine",
                        f"Intraday watchlist refresh: +{sorted(to_add)} −{sorted(to_remove)} "
                        f"(protected open positions: {sorted(open_symbols & existing)})",
                    )
                else:
                    applog.info("engine", "Intraday watchlist refresh: no changes")
            else:
                # Morning scan: full replacement (open positions still protected)
                protected = open_symbols & set(self.scanned_watchlist)
                final = set(new_watchlist) | protected
                self.scanned_watchlist = sorted(final)
                applog.info("engine", f"Watchlist dynamically updated by pre-market scanner: {new_watchlist}")

            self.watchlist = sorted(list(set(self.user_watchlist + self.scanned_watchlist)))

            if not is_intraday:
                # Only retrain model on the morning scan (30-day history)
                applog.info("engine", "Pre-market scan complete. Starting targeted warm-start training...")
                try:
                    from datetime import timedelta
                    from app.core.domain import utcnow
                    end = utcnow()
                    start = end - timedelta(days=30)

                    frames = {}
                    for symbol in new_watchlist:
                        df = await self.data.history(symbol, "1Min", start, end)
                        if not df.empty:
                            frames[symbol] = df
                    if frames:
                        info = await asyncio.to_thread(
                            self.model.train_adaptive, frames, self.settings.ml_horizon_bars, 0.10
                        )
                        applog.info("engine", f"Targeted daily adaptive model training completed: {info}")
                        self.publish("insight", self.insights.on_model_status(info).model_dump(mode="json"))

                        if self.portfolio_gate is not None:
                            try:
                                self.portfolio_gate.update_correlation_matrix(frames)
                            except Exception as exc:
                                applog.warning("engine", f"Failed to update portfolio correlation matrix: {exc}")
                    else:
                        applog.warning("engine", "No historical data for daily adaptive training.")
                except Exception as exc:
                    applog.error("engine", f"Pre-market daily adaptive model training failed: {exc}")

            self.publish("status", self.status())
            self.publish("quotes", self.quotes_payload())
        return new_watchlist

    async def run_event_monitor(self, now: datetime) -> None:
        from app.core.scanner import CANDIDATE_POOL
        from app.indicators.ta import enrich

        # Feature 4: Check economic calendar / earnings news and update event guard
        try:
            is_event, reason = await self.calendar.check(now, self.watchlist)
            if self.pipeline:
                self.pipeline.set_event_day(is_event)
                if is_event:
                    applog.info("engine", f"Event guard ACTIVE — score threshold raised to event level ({reason[:60]})")
        except Exception as exc:
            applog.debug("engine", f"Calendar check error (non-critical): {exc}")
        
        non_watchlist = [s for s in CANDIDATE_POOL if s not in self.watchlist]
        if not non_watchlist:
            return
            
        applog.info("engine", f"Running event monitor on {len(non_watchlist)} non-watchlist candidates...")
        
        start_time = now - timedelta(minutes=60)
        sem = asyncio.Semaphore(5)  # allow 5 concurrent; total ~200 req/min stays within Alpaca limit

        async def _check_symbol(symbol: str) -> str | None:
            async with sem:
                try:
                    await asyncio.sleep(0.06)  # 5 concurrent × 0.06s ≈ 200 req/min — within Alpaca rate limit
                    df = await self.data.history(symbol, "1Min", start_time, now)
                    if df.empty or len(df) < 15:
                        return None
                    
                    close_now = float(df["close"].iloc[-1])
                    open_5m = float(df["open"].iloc[-5])
                    ret_5m = abs(close_now / open_5m - 1.0) * 100.0
                    
                    vol_5m = float(df["volume"].iloc[-5:].sum())
                    avg_vol_1m = float(df["volume"].mean())
                    avg_vol_5m = avg_vol_1m * 5.0
                    rvol = vol_5m / avg_vol_5m if avg_vol_5m > 0 else 0.0
                    
                    # Thresholds: 1.5% in 5 min is a notable intraday move for any liquid US equity;
                    # 3x rvol vs same-session mean is a genuine volume surge (not the extreme 10x
                    # that empirically fires < once per month per symbol)
                    if ret_5m >= 1.5 or rvol >= 3.0:
                        df_enriched = enrich(df)
                        row = df_enriched.iloc[-1]
                        p_up = self.model.score_action(row, SignalAction.ENTER_LONG, symbol)
                        p_down = self.model.score_action(row, SignalAction.ENTER_SHORT, symbol)
                        max_prob = max(p_up, p_down)
                        
                        if max_prob >= 0.55:
                            applog.info(
                                "engine",
                                f"[Event Monitor] Symbol {symbol} triggered breakout. "
                                f"5m Return: {ret_5m:.1f}%, RVOL: {rvol:.1f}x, AI confidence: {max_prob:.2f}."
                            )
                            return symbol
                except Exception as _exc:
                    applog.debug(
                        "engine",
                        f"[Event Monitor] {symbol} skipped — transient error: {type(_exc).__name__}: {_exc}",
                    )
                return None

        tasks = [_check_symbol(sym) for sym in non_watchlist]
        results = await asyncio.gather(*tasks)
        triggered = [r for r in results if r is not None]
        
        if triggered:
            added_any = False
            for sym in triggered:
                if sym not in self.scanned_watchlist:
                    self.scanned_watchlist.append(sym)
                    added_any = True
                    self.publish("insight", {
                        "id": f"event-{sym}-{now.timestamp()}",
                        "ts": now.isoformat(),
                        "severity": "info",
                        "title": f"Momentum Surge: {sym}",
                        "detail": f"Added dynamically to watchlist. Volume Surge: {sym} surge detected. AI confidence: high."
                    })
            if added_any:
                self.watchlist = sorted(list(set(self.user_watchlist + self.scanned_watchlist)))
                if self.data is not None:
                    await self.data.warmup(triggered)
                self.publish("status", self.status())
                self.publish("quotes", self.quotes_payload())

    def _persist_gemini_shadow_observation(self, observation: dict[str, Any]) -> None:
        """Persist bounded research evidence without exposing it to execution code."""
        key = "gemini_shadow_observations"
        observations = self.db.get_setting(key) or []
        if not isinstance(observations, list):
            observations = []
        observations.append(observation)
        self.db.set_setting(key, observations[-200:])

    async def shutdown(self) -> None:
        await self.gemini_shadow.shutdown()
        await self.stop(flatten=False)
        if self.data is not None:
            await self.data.close()
        self.db.close()
