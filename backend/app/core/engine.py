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
    OrderType,
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
from app.strategies import SignalPipeline, Strategy, create_strategies, ParallelEvidenceEvaluator
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
        self.scanned_watchlist_ttl: dict[str, int] = {}
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
        self.resurrection_queue: list[dict] = []
        self._last_heartbeat_hour: int | None = None

        # Feature 4: Economic calendar / event guard
        from app.core.calendar import EventCalendar
        self.calendar = EventCalendar(alpaca_client=None)  # client attached in ensure_data()

        # Performance Engine & Portfolio Manager
        from app.portfolio.performance import StrategyPerformanceTracker
        self.perf_tracker = StrategyPerformanceTracker()
        self.portfolio_gate = None
        self.portfolio_allocator = None
        self.pending_reconciliation_orders: dict[str, dict] = db.get_setting("pending_reconciliation_orders") or {}
        self.reconciliation_failed = False

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

        restore_mode = None
        try:
            from app.brokers.alpaca import AlpacaBroker
            if isinstance(self.broker, AlpacaBroker) and mode in {"paper", "live"}:
                restore_mode = mode
        except Exception:
            pass
        self.portfolio = PortfolioTracker(restore_mode=restore_mode)

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
            self._reconcile_positions(positions)
        except Exception as exc:
            self.reconciliation_failed = True
            applog.error("engine", f"Failed to restore and reconcile active positions: {exc}")
        # Restore active promoted policy snapshot or threshold settings from the database (Fix 10)
        active_snapshot = self.db.get_setting("active_policy_snapshot")
        ml_thresh = self.risk_config.ml_confidence_threshold
        if active_snapshot and "ml_threshold" in active_snapshot:
            ml_thresh = float(active_snapshot["ml_threshold"])
            self.risk_config.ml_confidence_threshold = ml_thresh
            applog.info("engine", f"Restored active promoted policy snapshot on startup: {active_snapshot}")
            offset = float(active_snapshot.get("score_threshold_offset", 0.0))
            if offset != 0.0:
                self.risk_config.score_threshold_event = max(0.0, self.risk_config.score_threshold_event + offset)
                self.risk_config.score_threshold_trend = max(0.0, self.risk_config.score_threshold_trend + offset)
                self.risk_config.score_threshold_range = max(0.0, self.risk_config.score_threshold_range + offset)
                self.risk_config.score_threshold_volatile = max(0.0, self.risk_config.score_threshold_volatile + offset)
                applog.info("engine", f"Applied score threshold offset of {offset:+.1f} points to RiskConfig thresholds.")
        elif self.db.get_setting("ml_threshold") is not None:
            ml_thresh = float(self.db.get_setting("ml_threshold"))
            self.risk_config.ml_confidence_threshold = ml_thresh
            applog.info("engine", f"Restored active promoted ML threshold setting on startup: {ml_thresh}")

        self.pipeline = SignalPipeline(self.model, ml_thresh)
        self.evidence_evaluator = ParallelEvidenceEvaluator(
            model=self.model,
            ml_threshold=ml_thresh,
            meta_label_engine=self.pipeline.meta_label_engine,
            trade_score_engine=self.pipeline.trade_score_engine,
        )
        for s in self.strategies:
            s.reset()
        self._regimes.clear()
        self._flattened_sessions.clear()
        self._last_scan_date = None
        self._tick_count = 0
        self._consec_errors = 0
        self.last_error = ""

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

    # ------------------------------------------------------------------
    # the tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        assert self.data and self.broker and self.risk and self.portfolio and self.pipeline
        self._tick_count += 1
        now = utcnow()

        # Synchronize ML gate threshold with assessed market risk regime with default fallback validation
        is_stress = getattr(self.risk_config, "current_regime", "normal") in ("stress", "panic")
        raw_threshold = (
            getattr(self.risk_config, "stress_ml_confidence_threshold", 0.55)
            if is_stress
            else getattr(self.risk_config, "ml_confidence_threshold", 0.55)
        )
        try:
            import math
            val = float(raw_threshold)
            if math.isnan(val) or math.isinf(val) or not (0.0 <= val <= 1.0):
                val = 0.55
        except (ValueError, TypeError):
            val = 0.55
        self.pipeline.ml_threshold = val

        try:
            await self._reconcile_pending_submissions()
        except Exception as exc:
            applog.error("engine", f"Failed to reconcile pending submissions: {exc}")

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

        try:
            account = await self.broker.account()
            positions = await self.broker.positions()
            if getattr(self, "reconciliation_failed", False):
                applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")
                self._reconcile_positions(positions)
        except Exception as exc:
            self.reconciliation_failed = True
            applog.error("engine", f"Failed to fetch account/positions during tick: {exc}. Marking reconciliation uncertain.")
            from types import SimpleNamespace
            account = SimpleNamespace(
                equity=self.portfolio.equity,
                cash=self.portfolio.equity,
                buying_power=self.portfolio.equity * 4.0,
            )
            positions = []

        if self.state == EngineState.RUNNING:
            self._run_heartbeat_tasks(now)

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
        elif halt == "daily" or (self.risk.daily_halted and not self.risk.hard_halted):
            await self._reconcile_daily_halt_liquidation(now)
            if halt == "daily":
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

            # Process resurrected signals from queue (CR-146 Phase 4)
            # SAFETY: Resurrection uses stale signal price/SL/TP from the original rejection tick.
            # Enabled only for paper/shadow sessions. Live sessions skip entirely to avoid stale orders.
            _resurrection_enabled = getattr(self.broker, "is_paper", False) is True
            resurrected = []
            if _resurrection_enabled:
                resurrected = await self._process_resurrection_queue(account.equity, now)
            elif self.resurrection_queue:
                applog.debug(
                    "engine.resurrection",
                    f"Resurrection queue skipped for live session ({len(self.resurrection_queue)} items pending). "
                    "Clearing stale intents."
                )
                self.resurrection_queue.clear()
            candidates.extend(resurrected)

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
                    if getattr(sig, "candidate_id", None) is not None:
                        self._update_shadow_decision(sig.candidate_id, "rejected", reason, "portfolio_gate")

                # Size and submit approved entries
                if decision_outcome.approved_entries:
                    if getattr(self, "reconciliation_failed", False):
                        applog.warning("engine", "Skipping entry sizing and submission while reconciliation is uncertain.")
                        for sig, _ in decision_outcome.approved_entries:
                            if getattr(sig, "candidate_id", None) is not None:
                                self._update_shadow_decision(
                                    sig.candidate_id,
                                    "rejected",
                                    "engine position/broker reconciliation state is uncertain",
                                    "risk_gate",
                                )
                    else:
                        open_orders = await self.broker.open_orders()
                        pending_entries = [
                            o.symbol.upper() for o in open_orders
                            if is_entry_order(o, positions)
                        ]
                        effective_pending_entries = set(pending_entries)
                        for info in self.pending_reconciliation_orders.values():
                            effective_pending_entries.add(info["symbol"].upper())
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
                                if getattr(sig, "candidate_id", None) is not None:
                                    self._update_shadow_decision(sig.candidate_id, "rejected", decision.reason, "risk_gate")
                                continue

                            client_order_id = decision.order.client_order_id
                            self.pending_reconciliation_orders[client_order_id] = {
                                "symbol": decision.order.symbol.upper(),
                                "client_order_id": client_order_id,
                                "side": str(decision.order.side),
                                "qty": decision.order.qty,
                                "order_type": str(decision.order.order_type),
                                "limit_price": decision.order.limit_price,
                                "submitted_at": utcnow().isoformat(),
                            }
                            self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)

                            try:
                                order = await self.broker.submit(decision.order)
                                self.pending_reconciliation_orders.pop(client_order_id, None)
                                self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                                self.recent_orders.appendleft(order)
                                self.db.save_order(order, self.mode)
                                self.publish("insight", self.insights.on_signal(sig, gated=False).model_dump(mode="json"))
                                self.publish("orders", self.orders_payload())
                                if getattr(sig, "candidate_id", None) is not None:
                                    if order.status == OrderStatus.REJECTED:
                                        self._update_shadow_decision(
                                            sig.candidate_id,
                                            "rejected",
                                            order.reason or "Broker rejected order",
                                            "broker_rejected"
                                        )
                                    else:
                                        self._update_shadow_decision(sig.candidate_id, "accepted", "", "none")
                            except Exception as exc:
                                applog.error("engine", f"Ambiguous submission exception for {decision.order.symbol} ({client_order_id}): {exc}")
                                order = Order(
                                    id=client_order_id,
                                    client_order_id=client_order_id,
                                    symbol=decision.order.symbol,
                                    side=decision.order.side,
                                    qty=decision.order.qty,
                                    order_type=decision.order.order_type,
                                    limit_price=decision.order.limit_price,
                                    status=OrderStatus.PENDING,
                                    reason=f"Ambiguous submit: {exc}"
                                )
                                self.db.save_order(order, self.mode)

                            # Reserve active same-cycle submissions before evaluating the next candidate.
                            # This overlays the stale broker snapshot for cash, position-count, and sector checks.
                            reference_price = sig.price or float(df["close"].iloc[-1])
                            if reservations.reserve(order, reference_price):
                                effective_pending_entries.add(sig.symbol.upper())

            await self._drain_fills(now)

        # 5.5 Periodic background replay of pending shadow signals
        if self._tick_count % 10 == 0:
            try:
                await self._replay_pending_signals(now)
            except Exception as e:
                applog.error("engine", f"Background replay error: {e}")

        # 6. mark equity + persist
        if not getattr(self, "reconciliation_failed", False) and new_closed:
            try:
                account = await self.broker.account()
            except Exception as exc:
                self.reconciliation_failed = True
                applog.error("engine", f"Failed to fetch account during mark equity: {exc}")

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
                    atr = float(df["atr14"].iloc[-1]) if "atr14" in df.columns and len(df) > 0 and pd.notna(df["atr14"].iloc[-1]) else 0.0
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
                sig.strategy_params = getattr(strat, "params", {})
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

        # Assign a unique candidate ID to all candidate entry signals first (shared UUID)
        import uuid
        for sig in signals:
            if sig.action != SignalAction.EXIT:
                if getattr(sig, "candidate_id", None) is None:
                    prefix = f"{symbol}_{sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}"
                    sig.candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()

        # Compute slot_rvol_val for the current symbol
        slot_rvol_val: float | None = None
        try:
            slot_engine = SlotVolumeEngine(df, lookback_sessions=20, min_observations=5)
            slot_rvol_val = slot_engine.get_slot_rvol(now)
        except Exception:
            slot_rvol_val = None

        # Estimate SPY 30-bar rolling volatility (standard deviation of returns)
        spy_vol: float | None = None
        if spy_df is not None and not spy_df.empty:
            try:
                spy_vol = float(spy_df["close"].pct_change().rolling(30).std().iloc[-1] * 100)
            except Exception:
                spy_vol = None

        # Fetch Level 1 Context expectancy for each strategy and log/adapt (CR-146 Phase 5)
        # SAFETY: adaptive_scores is a RESEARCH-ZONE side-dict. sig.score is NEVER mutated here.
        # The Champion's SignalPipeline always receives the original, unmodified sig object.
        from app.strategies import get_contextual_expectancy, get_adaptive_ml_threshold, get_adaptive_signal_score
        from app.core.phase import classify_clock_phase
        clock_phase = classify_clock_phase(now)
        adaptive_ml_thresholds: dict[str, float] = {}
        adaptive_scores: dict[str, float] = {}  # research-zone only — never written back to sig
        for sig in signals:
            if sig.action != SignalAction.EXIT:
                try:
                    exp_info = get_contextual_expectancy(
                        db=self.db,
                        strategy=sig.strategy,
                        direction="long" if sig.action == SignalAction.ENTER_LONG else "short",
                        regime=regime.name if hasattr(regime, "name") else str(regime),
                        clock_phase=clock_phase.value if hasattr(clock_phase, "value") else str(clock_phase),
                        rvol=slot_rvol_val,
                        spy_vol=spy_vol,
                    )
                    applog.info(
                        "engine.memory",
                        f"Context memory lookup for {sig.symbol} {sig.strategy} {sig.action}: "
                        f"trades_count={exp_info['trades_count']}, expectancy={exp_info['expectancy']}, "
                        f"shrunk_expectancy={exp_info['shrunk_expectancy']} (source: {exp_info['source']})"
                    )

                    # Compute adaptive ML threshold (research-zone, fed only to ParallelEvidenceEvaluator)
                    dyn_ml_threshold = get_adaptive_ml_threshold(self.pipeline.ml_threshold, exp_info)
                    adaptive_ml_thresholds[sig.candidate_id] = dyn_ml_threshold

                    # Compute adaptive score (research-zone, stored in side-dict — NOT written back to sig)
                    if sig.score is not None:
                        adaptive_scores[sig.candidate_id] = get_adaptive_signal_score(sig.score, exp_info)
                except Exception as exc:
                    applog.error("engine.memory", f"Failed memory expectancy lookup / adaptation: {exc}")

        # Run ParallelEvidenceEvaluator out-of-band to capture the parallel evidence (CR-146)
        parallel_evidences = {}
        for sig in signals:
            if sig.action != SignalAction.EXIT:
                try:
                    midday_active = getattr(self.pipeline, "_midday_active", False)
                    is_event_day = getattr(self.pipeline, "_is_event_day", False)
                    evidence = self.evidence_evaluator.evaluate(
                        symbol=symbol,
                        df=df,
                        sig=sig,
                        regime=regime,
                        sector_strength=rs_score,
                        is_event_day=is_event_day,
                        midday_active=midday_active,
                        ml_threshold=adaptive_ml_thresholds.get(sig.candidate_id),
                    )
                    if evidence:
                        parallel_evidences[sig.candidate_id] = evidence
                except Exception as exc:
                    applog.error("engine.evaluator", f"ParallelEvidenceEvaluator failed for {symbol} {sig.strategy}: {exc}")

        accepted, rejected = self.pipeline.process(symbol, df, signals, regime, sector_strength=rs_score)
        
        # Save signals to shadow ledger
        for sig in signals:
            if sig.action == SignalAction.EXIT:
                continue
            is_accepted = sig in accepted
            final_decision = "pipeline_approved" if is_accepted else "rejected"
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
                elif "Meta-label" in rejection_reason:
                    rejection_stage = "meta_label_gate"
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
                candidate_id=sig.candidate_id,
                strategy_params=getattr(sig, "strategy_params", {}),
                policy_id="champion",
                parent_policy_id=None,
                policy_version="1.0.0",
                gate_evidence_json=parallel_evidences.get(sig.candidate_id),
                spy_vol=spy_vol,
            )

            # Run and record all active Challengers (CR-146 Phase 3)
            # SAFETY: Each challenger receives an independent deep copy of the original signal
            # so Champion's sig object is never shared with or contaminated by Challenger paths.
            from app.strategies.challenger import get_active_challengers
            for challenger in get_active_challengers():
                try:
                    challenger_sig = sig.model_copy(deep=True)  # pristine deep copy for this challenger
                    challenger_eval = ParallelEvidenceEvaluator(
                        model=self.model,
                        ml_threshold=challenger.ml_threshold,
                        meta_label_engine=self.pipeline.meta_label_engine,
                        trade_score_engine=challenger.create_custom_trade_score_engine(self.pipeline.trade_score_engine),
                    )
                    c_evidence = challenger_eval.evaluate(
                        symbol=symbol,
                        df=df,
                        sig=challenger_sig,
                        regime=regime,
                        sector_strength=rs_score,
                        is_event_day=is_event_day,
                        midday_active=midday_active,
                    )
                    
                    c_vetoes = c_evidence.get("vetoes", {})
                    c_accepted = len(c_vetoes) == 0
                    c_final_decision = "pipeline_approved" if c_accepted else "rejected"
                    
                    c_rejection_stage = "none"
                    c_rejection_reason = ""
                    if not c_accepted:
                        stage = list(c_vetoes.keys())[0]
                        c_rejection_stage = stage
                        c_rejection_reason = c_vetoes[stage]

                    ShadowLedger.record_candidate(
                        db_engine=self.db,
                        symbol=symbol,
                        ts=now,
                        strategy=challenger_sig.strategy,
                        direction="long" if challenger_sig.action == SignalAction.ENTER_LONG else "short",
                        signal_price=challenger_sig.price or float(df["close"].iloc[-1]),
                        stop_loss=challenger_sig.stop_loss,
                        take_profit=challenger_sig.take_profit,
                        ml_confidence=challenger_sig.ml_probability,
                        final_decision=c_final_decision,
                        rejection_stage=c_rejection_stage,
                        rejection_reason=c_rejection_reason,
                        df=df,
                        spy_df=spy_df if spy_df is not None else pd.DataFrame(),
                        slot_rvol_val=slot_rvol_val,
                        vwap_telemetry=vwap_tel,
                        mode="challenger",
                        candidate_id=sig.candidate_id,
                        strategy_params=getattr(sig, "strategy_params", {}),
                        policy_id=challenger.policy_id,
                        parent_policy_id="champion",
                        policy_version="1.0.0",
                        gate_evidence_json=c_evidence,
                        spy_vol=spy_vol,
                    )
                except Exception as exc:
                    applog.error("engine.challenger", f"Challenger {challenger.policy_id} failed for {symbol}: {exc}")

        _resurrection_enabled = getattr(self.broker, "is_paper", False) is True
        for sig, reason in rejected:
            applog.debug("pipeline", f"{symbol} {sig.strategy} {sig.action}: rejected -- {reason}")
            if sig.action != SignalAction.EXIT and _resurrection_enabled:
                in_queue = any(
                    q["symbol"] == sig.symbol and q["strategy"] == sig.strategy
                    for q in self.resurrection_queue
                )
                if not in_queue:
                    # Enforce defensive queue bound (e.g. max 50 items)
                    if len(self.resurrection_queue) >= 50:
                        applog.warning("engine.resurrection", "Resurrection queue is full (max 50). Discarding oldest intent.")
                        self.resurrection_queue.pop(0)

                    # Store resurrection INTENT only — not the full sig object.
                    # Stale price/SL/TP are intentionally excluded to prevent stale-data orders.
                    # When retried, the live pipeline will gate fresh market data through all gates.
                    self.resurrection_queue.append({
                        "symbol": sig.symbol,
                        "strategy": sig.strategy,
                        "direction": "long" if sig.action == SignalAction.ENTER_LONG else "short",
                        "candidate_id": sig.candidate_id,
                        "original_rejection_reason": reason,
                        "ts_rejected": now,
                        "retry_count": 0,
                    })
                    applog.info(
                        "engine.resurrection",
                        f"Added rejected candidate {sig.symbol} {sig.strategy} to resurrection queue (rejection: {reason})"
                    )

        candidate_entries = []
        for sig in accepted:
            if sig.action == SignalAction.EXIT:
                if position is not None:
                    applog.info("engine", f"{symbol}: strategy exit -- {sig.reason}")
                    await self._close_position_guarded(symbol, reason=sig.reason)
                continue
            
            if getattr(self, "reconciliation_failed", False):
                applog.warning("engine", f"{symbol}: entry signal {sig.strategy} rejected -- engine position/broker reconciliation state is uncertain")
                continue

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
        order_by_id = {}
        for o in self.recent_orders:
            order_by_id[o.id] = o
            if o.client_order_id:
                order_by_id[o.client_order_id] = o

        for fill in fills:
            order = order_by_id.get(fill.order_id)
            if order is None and fill.order_id:
                order = self.db.get_order(fill.order_id)

            if order is not None and order.status != OrderStatus.FILLED:
                # Paper orders share objects and are already updated; Alpaca
                # orders are snapshots that need their fill state applied.
                order.status = OrderStatus.FILLED
                order.filled_qty = fill.qty
                order.filled_avg_price = fill.price
                order.filled_at = fill.ts
            
            symbol = fill.symbol.upper()
            # Release pending-close guard when sell fill arrives
            if fill.side == OrderSide.SELL:
                self._pending_close_symbols.discard(symbol)
            peak = self._position_peaks.get(symbol)
            trough = self._position_troughs.get(symbol)
            
            init_stop = order.stop_loss if order is not None else None
            if init_stop is None:
                init_stop = self._position_initial_stops.get(symbol)
            else:
                self._position_initial_stops[symbol] = init_stop

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
            "reconciliation_failed": getattr(self, "reconciliation_failed", False),
        }

    def _reconcile_positions(self, positions: list[Position]) -> None:
        """Reconcile and reconstruct database portfolio lot states from broker positions."""
        self.portfolio._lots.clear()
        for p in positions:
            strat = "manual"
            if hasattr(self.broker, "_symbol_strategies"):
                db_strat = self.db.get_latest_order_strategy(p.symbol, self.mode)
                if db_strat:
                    self.broker._symbol_strategies[p.symbol] = db_strat
                    strat = db_strat
                    applog.info("engine", f"Restored strategy '{db_strat}' for active position {p.symbol}")

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
        self.reconciliation_failed = False

    async def _reconcile_pending_submissions(self) -> None:
        """Query the broker for ambiguous order submissions and resolve their status."""
        if not self.pending_reconciliation_orders:
            return

        client_ids = list(self.pending_reconciliation_orders.keys())
        applog.info("engine", f"Checking status of {len(client_ids)} ambiguous order submissions: {client_ids}")

        for cid in client_ids:
            order_info = self.pending_reconciliation_orders[cid]
            symbol = order_info["symbol"]
            try:
                order = await self.broker.get_order_by_client_id(cid)
                if order is not None:
                    applog.info("engine", f"Ambiguous order resolved: {symbol} ({cid}) is active/filled at broker.")
                    self.pending_reconciliation_orders.pop(cid, None)
                    self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                    self.db.save_order(order, self.mode)
                else:
                    applog.info("engine", f"Ambiguous order resolved: {symbol} ({cid}) was NEVER received by broker. Treating as REJECTED.")
                    self.pending_reconciliation_orders.pop(cid, None)
                    self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                    rejected_order = Order(
                        id=cid,
                        client_order_id=cid,
                        symbol=symbol,
                        side=OrderSide.BUY if order_info["side"].lower() == "buy" else OrderSide.SELL,
                        qty=order_info["qty"],
                        order_type=OrderType.LIMIT if order_info["order_type"].lower() == "limit" else OrderType.MARKET,
                        limit_price=order_info["limit_price"],
                        status=OrderStatus.REJECTED,
                        reason="Ambiguous submit resolved to not found at broker."
                    )
                    self.db.save_order(rejected_order, self.mode)
            except Exception as exc:
                applog.warning("engine", f"Reconciliation query failed for client ID {cid}: {exc}. Retaining guard.")

    def _update_shadow_decision(self, candidate_id: str, final_decision: str, rejection_reason: str, rejection_stage: str) -> None:
        """Update a candidate signal's gating decision in the database."""
        if not self.db:
            return
        try:
            payload = self.db.get_shadow_signal(candidate_id)
            if payload:
                payload["final_decision"] = final_decision
                payload["rejection_reason"] = rejection_reason
                payload["rejection_stage"] = rejection_stage
                
                ts = datetime.fromisoformat(payload["timestamp"])
                symbol = payload["symbol"]
                strategy = payload["strategy"]
                direction = payload["direction"]
                
                self.db.save_shadow_signal(
                    candidate_id=candidate_id,
                    ts=ts,
                    symbol=symbol,
                    strategy=strategy,
                    direction=direction,
                    mode=self.mode,
                    payload=payload,
                )
        except Exception as exc:
            applog.error("engine", f"Failed to update shadow signal decision {candidate_id}: {exc}")

    async def _replay_pending_signals(self, now: datetime) -> None:
        """Scan SQLite database for pending rejected signals, replay them, and update records."""
        if not self.db or not self.data:
            return

        import numpy as np
        try:
            signals = self.db.get_shadow_signals(mode=self.mode, limit=300)
            pending = []
            for s in signals:
                if s.get("final_decision") == "rejected" and not s.get("replayed"):
                    try:
                        ts = datetime.fromisoformat(s["timestamp"])
                        if now - ts >= timedelta(minutes=60) and ts >= now - timedelta(days=7):
                            if s.get("replay_attempts", 0) < 5:
                                pending.append(s)
                    except Exception:
                        pass

            if not pending:
                return

            pending.sort(
                key=lambda x: (
                    x.get("timestamp", ""),
                    x.get("ml_confidence") or 0.0,
                    x.get("score") or 0.0
                ),
                reverse=True
            )

            batch = pending[:5]
            applog.info("engine", f"Found {len(pending)} pending signals for replay. Executing batch of {len(batch)}.")

            for s in batch:
                candidate_id = s["candidate_id"]
                symbol = s["symbol"]
                ts = datetime.fromisoformat(s["timestamp"])
                direction = s["direction"]
                stop_loss = s.get("stop_loss")
                take_profit = s.get("take_profit")
                entry_price = s.get("entry_price") or s.get("signal_price")
                strategy_name = s.get("strategy")
                strategy_params = s.get("strategy_params")

                s["replay_attempts"] = s.get("replay_attempts", 0) + 1

                start_time = ts - timedelta(minutes=15)
                end_time = ts + timedelta(minutes=90)
                
                applog.info("engine", f"Replaying signal {candidate_id} ({symbol} {direction}) at {ts.isoformat()}...")
                
                try:
                    df = await self.data.history(symbol, "1Min", start=start_time, end=end_time)
                except Exception as df_exc:
                    applog.warning("engine", f"Failed to fetch history for replay of {candidate_id}: {df_exc}")
                    self.db.save_shadow_signal(candidate_id, ts, symbol, strategy_name, direction, self.mode, s)
                    continue

                if df.empty:
                    applog.warning("engine", f"History empty for replay of {candidate_id}. Skipping.")
                    self.db.save_shadow_signal(candidate_id, ts, symbol, strategy_name, direction, self.mode, s)
                    continue

                ts_pd = pd.Timestamp(ts).tz_convert("UTC")
                i = -1
                if ts_pd in df.index:
                    i = df.index.get_loc(ts_pd)
                else:
                    diffs = np.abs(df.index - ts_pd)
                    min_diff_idx = diffs.argmin()
                    if diffs[min_diff_idx] <= pd.Timedelta(minutes=2):
                        i = min_diff_idx

                if i == -1 or i + 1 >= len(df):
                    applog.warning("engine", f"Cannot locate entry bar index for replay of {candidate_id} (i={i}). Skipping.")
                    self.db.save_shadow_signal(candidate_id, ts, symbol, strategy_name, direction, self.mode, s)
                    continue

                replay_res = ShadowLedger.replay_signal_60m(
                    df=df,
                    entry_idx=i + 1,
                    direction=direction,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    entry_price=entry_price,
                    strategy_name=strategy_name,
                    strategy_params=strategy_params,
                )

                if not replay_res:
                    applog.warning("engine", f"Replay simulation returned empty result for {candidate_id}.")
                    self.db.save_shadow_signal(candidate_id, ts, symbol, strategy_name, direction, self.mode, s)
                    continue

                s["replayed"] = True
                s["replay_timestamp"] = utcnow().isoformat()
                s["simulated_pnl"] = replay_res["pnl_pct"]
                s["mfe_pct"] = replay_res["mfe_pct"]
                s["mae_pct"] = replay_res["mae_pct"]
                s["exit_reason"] = replay_res["exit_reason"]
                s["tp_hit"] = replay_res["tp_hit"]
                s["sl_hit"] = replay_res["sl_hit"]
                s["replay_60m"] = replay_res

                # Alternative exits simulation (chronological chronological replaying)
                alt_outcomes = {}
                alt_outcomes["exit_production"] = {
                    "pnl_pct": replay_res["pnl_pct"],
                    "realized_r": replay_res.get("realized_r", 0.0),
                    "exit_reason": replay_res["exit_reason"],
                    "tp_hit": replay_res["tp_hit"],
                    "sl_hit": replay_res["sl_hit"],
                    "bars_held": replay_res["bars_held"],
                    "mfe_r": replay_res.get("mfe_r", 0.0),
                    "mae_r": replay_res.get("mae_r", 0.0),
                }

                alt_policies = getattr(self.risk_config, "alternative_exits", [])
                for alt in alt_policies:
                    alt_policy_id = alt.get("policy_id")
                    if not alt_policy_id:
                        continue
                    
                    alt_tp = None
                    if "take_profit_pct" in alt:
                        alt_tp = entry_price * (1.0 + alt["take_profit_pct"]/100.0 if direction == "long" else 1.0 - alt["take_profit_pct"]/100.0)
                    alt_sl = None
                    if "stop_loss_pct" in alt:
                        alt_sl = entry_price * (1.0 - alt["stop_loss_pct"]/100.0 if direction == "long" else 1.0 + alt["stop_loss_pct"]/100.0)
                    
                    alt_params = {**strategy_params} if strategy_params else {}
                    if "trailing_stop_atr_mult" in alt:
                        alt_params["trailing_stop_atr_mult"] = alt["trailing_stop_atr_mult"]
                    
                    alt_res = ShadowLedger.replay_signal_60m(
                        df=df,
                        entry_idx=i + 1,
                        direction=direction,
                        stop_loss=alt_sl or stop_loss,
                        take_profit=alt_tp or take_profit,
                        entry_price=entry_price,
                        strategy_name=strategy_name,
                        strategy_params=alt_params,
                    )
                    
                    if alt_res:
                        alt_outcomes[alt_policy_id] = {
                            "pnl_pct": alt_res["pnl_pct"],
                            "realized_r": alt_res.get("realized_r", 0.0),
                            "exit_reason": alt_res["exit_reason"],
                            "tp_hit": alt_res["tp_hit"],
                            "sl_hit": alt_res["sl_hit"],
                            "bars_held": alt_res["bars_held"],
                            "mfe_r": alt_res.get("mfe_r", 0.0),
                            "mae_r": alt_res.get("mae_r", 0.0),
                        }
                s["alternative_exits"] = alt_outcomes

                if not s.get("outcomes") or not s["outcomes"]:
                    try:
                        s["outcomes"] = ShadowLedger.calculate_forward_outcomes(
                            df, i, direction, stop_loss, replay_res["entry_price"]
                        )
                    except Exception:
                        pass

                # Calculate unique/shared/marginal veto attribution at replay time
                gate_evidence = s.get("gate_evidence_json")
                if gate_evidence and "vetoes" in gate_evidence:
                    vetoes = gate_evidence["vetoes"]
                    realized_r = 0.0
                    if s.get("outcomes"):
                        if "15m" in s["outcomes"]:
                            realized_r = s["outcomes"]["15m"].get("realized_r", 0.0)
                        elif "close" in s["outcomes"]:
                            realized_r = s["outcomes"]["close"].get("realized_r", 0.0)
                    else:
                        realized_r = replay_res.get("realized_r", 0.0)

                    from app.strategies.evaluator import ParallelEvidenceEvaluator
                    attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, realized_r)
                    s.update(attrib)

                self.db.save_shadow_signal(
                    candidate_id=candidate_id,
                    ts=ts,
                    symbol=symbol,
                    strategy=strategy_name,
                    direction=direction,
                    mode=self.mode,
                    payload=s,
                )
                applog.info("engine", f"Replay successful for {candidate_id}: P&L={replay_res['pnl_pct']}% via {replay_res['exit_reason']}")
        except Exception as exc:
            applog.error("engine", f"Failed in _replay_pending_signals loop: {exc}")

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
        
        # Calculate clean base risk configuration by stripping active policy offset
        active_snapshot = self.db.get_setting("active_policy_snapshot")
        offset = float(active_snapshot.get("score_threshold_offset", 0.0)) if active_snapshot else 0.0
        base_risk = self.risk_config.model_dump()
        if offset != 0.0:
            for key in ["score_threshold_event", "score_threshold_trend", "score_threshold_range", "score_threshold_volatile"]:
                base_risk[key] = max(0.0, float(base_risk[key]) - offset)
                
        self.db.set_setting("risk_config", base_risk)
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
            new_watchlist = []

        if outcome in (ScannerOutcome.SUCCESS_NONEMPTY, ScannerOutcome.SUCCESS_EMPTY):
            # Feature 1: Hysteresis — protect symbols with open positions & pending orders from removal
            open_symbols: set[str] = set()
            pending_symbols: set[str] = set()
            try:
                if self.broker:
                    positions = await self.broker.positions()
                    open_symbols = {p.symbol.upper() for p in positions}
                    open_orders = await self.broker.open_orders()
                    pending_symbols = {o.symbol.upper() for o in open_orders}
                active_trade_symbols = open_symbols | pending_symbols
            except Exception as exc:
                applog.error(
                    "engine",
                    f"Failed to retrieve broker positions/orders during scan: {exc}. "
                    "Fail-safe: preserving the pre-update scanned watchlist without changes.",
                )
                return self.watchlist

            if is_intraday:
                # Intraday: only add new symbols; retain all existing except those
                # that dropped off scanner AND have no active exposure and not retained by TTL
                retained_dynamic = {sym for sym, ttl in self.scanned_watchlist_ttl.items() if ttl > 0}
                existing = set(self.scanned_watchlist)
                new_set = set(new_watchlist)
                # Symbols to remove: in existing but not in new scan, no active exposure AND not retained by TTL
                to_remove = (existing - new_set) - (active_trade_symbols | retained_dynamic)
                # Symbols to add: in new scan but not yet in existing
                to_add = new_set - existing
                updated = (existing - to_remove) | to_add
                self.scanned_watchlist = sorted(updated)
                if to_add or to_remove:
                    applog.info(
                        "engine",
                        f"Intraday watchlist refresh: +{sorted(to_add)} −{sorted(to_remove)} "
                        f"(protected exposure: {sorted((active_trade_symbols | retained_dynamic) & existing)})",
                    )
                else:
                    applog.info("engine", "Intraday watchlist refresh: no changes")
            else:
                # Morning scan: full replacement (active exposures and TTL-retained symbols protected)
                scanned_set = set(new_watchlist)
                # Reset TTL for today's scanned symbols
                for sym in scanned_set:
                    self.scanned_watchlist_ttl[sym] = self.risk_config.scanner_watchlist_ttl
                # Decrement TTL for previously scanned symbols that were not scanned today
                for sym in list(self.scanned_watchlist_ttl.keys()):
                    if sym not in scanned_set:
                        self.scanned_watchlist_ttl[sym] = self.scanned_watchlist_ttl.get(sym, 1) - 1
                
                retained_dynamic = {sym for sym, ttl in self.scanned_watchlist_ttl.items() if ttl > 0}
                final = scanned_set | retained_dynamic | active_trade_symbols
                self.scanned_watchlist = sorted(final)
                applog.info(
                    "engine",
                    f"Watchlist dynamically updated by pre-market scanner: {new_watchlist} "
                    f"(retained: {sorted(retained_dynamic)}, active protection: {sorted(active_trade_symbols)})",
                )

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

    async def _process_resurrection_queue(self, account_equity: float, now: datetime) -> list[tuple[Signal, pd.DataFrame]]:
        """Processes the resurrection queue, re-evaluating vetoed signals.
        Returns a list of resurrected candidate signal tuples (sig, df).
        """
        if not self.resurrection_queue:
            return []

        resurrected = []
        active_queue = []
        TTL_MINUTES = 30
        MAX_RETRIES = 3

        from datetime import UTC
        for item in self.resurrection_queue:
            ts_rejected = item["ts_rejected"]
            dt_now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
            dt_rejected = ts_rejected.astimezone(UTC) if ts_rejected.tzinfo else ts_rejected.replace(tzinfo=UTC)
            if (dt_now - dt_rejected).total_seconds() / 60.0 > TTL_MINUTES:
                applog.info("engine.resurrection", f"Resurrected candidate {item['symbol']} expired (TTL {TTL_MINUTES}m hit).")
                continue

            if item["retry_count"] >= MAX_RETRIES:
                applog.info("engine.resurrection", f"Resurrected candidate {item['symbol']} reached max retries ({MAX_RETRIES}).")
                continue

            symbol = item["symbol"]
            strategy_name = item["strategy"]

            # Find the originating strategy
            strat = None
            for s in self.strategies:
                if s.name == strategy_name:
                    strat = s
                    break

            if strat is None or not strat.enabled:
                applog.warning("engine.resurrection", f"Strategy {strategy_name} not found or disabled for resurrection.")
                continue

            try:
                # Fetch recent window
                df = enrich(self.data.frame(symbol, "1Min", limit=600))
                if df is None or df.empty:
                    active_queue.append(item)
                    continue

                # Run strategy on fresh bar
                pos_by_symbol = {p.symbol: p for p in self.portfolio.positions} if self.portfolio else {}
                spy_df = enrich(self.data.frame("SPY", "1Min", limit=600))
                rs_score = compute_relative_strength(df, spy_df)
                regime, _ = detect_regime(df)

                from app.strategies.base import StrategyContext
                sctx = StrategyContext(position=pos_by_symbol.get(symbol), equity=account_equity, regime=regime, now=now)
                fresh_sig = strat.on_bar(sctx, symbol, df)
                if fresh_sig is None:
                    applog.debug("engine.resurrection", f"{symbol} {strategy_name}: Strategy no longer produces a signal on current bar.")
                    continue

                # Verify direction matches original intent
                expected_action = SignalAction.ENTER_LONG if item["direction"] == "long" else SignalAction.ENTER_SHORT
                if fresh_sig.action != expected_action:
                    applog.debug("engine.resurrection", f"{symbol} {strategy_name}: Fresh signal action {fresh_sig.action} does not match original direction.")
                    continue

                # Assign fresh candidate ID and link to the parent candidate ID
                import uuid
                prefix = f"{symbol}_{fresh_sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}"
                fresh_sig.candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()
                fresh_sig.parent_candidate_id = item["candidate_id"]
                fresh_sig.strategy_params = getattr(strat, "params", {})

                # Re-run pipeline process on fresh signal
                accepted, rejected = self.pipeline.process(symbol, df, [fresh_sig], regime, sector_strength=rs_score)

                if fresh_sig in accepted:
                    item["retry_count"] += 1
                    applog.info(
                        "engine.resurrection",
                        f"SUCCESS: Resurrected candidate {symbol} {fresh_sig.strategy} {fresh_sig.action} passed all gates! "
                        f"Submitting for allocation (retry {item['retry_count']}/{MAX_RETRIES})."
                    )

                    self._update_shadow_decision(
                        candidate_id=fresh_sig.candidate_id,
                        final_decision="pipeline_approved",
                        rejection_reason="",
                        rejection_stage="none",
                    )
                    # Also update the parent candidate record to mark it as resurrected/approved
                    if "candidate_id" in item:
                        self._update_shadow_decision(
                            candidate_id=item["candidate_id"],
                            final_decision="pipeline_approved",
                            rejection_reason="",
                            rejection_stage="none",
                        )

                    resurrected.append((fresh_sig, df))
                else:
                    item["retry_count"] += 1
                    active_queue.append(item)
            except Exception as exc:
                applog.error("engine.resurrection", f"Failed re-evaluation of {symbol}: {exc}")
                active_queue.append(item)

        self.resurrection_queue = active_queue
        return resurrected

    def _run_heartbeat_tasks(self, now: datetime) -> None:
        """Executes periodic heartbeat tasks (e.g. dynamic universe refresh based on sector leadership)."""
        current_hour = now.hour
        if self._last_heartbeat_hour == current_hour:
            return
        
        self._last_heartbeat_hour = current_hour
        applog.info("engine.heartbeat", f"Running hourly sector leadership heartbeat check (hour: {current_hour})...")

        try:
            sector_etfs = {
                "XLK": "Technology",
                "XLF": "Financials",
                "XLE": "Energy",
                "XLV": "Healthcare",
                "XLY": "Consumer Discretionary",
                "XLI": "Industrials",
                "XLP": "Consumer Staples",
                "XLB": "Materials",
                "XLU": "Utilities",
            }
            
            sector_returns = {}
            for etf in sector_etfs:
                etf_df = enrich(self.data.frame(etf, "1Min", limit=390))
                if etf_df is not None and len(etf_df) > 1:
                    close_first = float(etf_df["close"].iloc[0])
                    close_last = float(etf_df["close"].iloc[-1])
                    ret = (close_last - close_first) / close_first if close_first > 0 else 0.0
                    sector_returns[etf] = ret

            if sector_returns:
                sorted_sectors = sorted(sector_returns.items(), key=lambda x: x[1], reverse=True)
                top_sectors = sorted_sectors[:3]
                applog.info(
                    "engine.heartbeat",
                    f"Sector Leadership Ranks: {', '.join([f'{etf} ({ret:.2%})' for etf, ret in sorted_sectors])}"
                )
                applog.info(
                    "engine.heartbeat",
                    f"Dynamic universe focus refreshed. Top sectors: {', '.join([etf for etf, _ in top_sectors])}."
                )
        except Exception as exc:
            applog.error("engine.heartbeat", f"Failed sector leadership heartbeat check: {exc}")

    async def shutdown(self) -> None:
        await self.gemini_shadow.shutdown()
        await self.stop(flatten=False)
        if self.data is not None:
            await self.data.close()
        self.db.close()
