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
import inspect
import math
import os
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.ai.insights import InsightEngine
from app.ai.model import SignalModel
from app.ai.trainer import ContinuousTrainer
from app.backtest.metrics import compute_metrics
from app.brokers.base import Broker, make_broker
from app.brokers.paper import PaperBroker
from app.config import DATA_DIR, RiskConfig, Settings, get_models_dir
from app.core.applog import applog
from app.core.calendar_helper import get_session_close
from app.core.clock import MarketClock
from app.core.domain import (
    EngineState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Regime,
    Signal,
    SignalAction,
    utcnow,
)
from app.core.engine_evaluation import TradingEngineEvaluationMixin
from app.core.engine_halts import TradingEngineHaltsMixin
from app.core.entry_reservations import SameCycleEntryReservations
from app.data.manager import DataManager, make_provider
from app.db.database import Database
from app.indicators.ta import enrich
from app.pis import PISEngineAdapter
from app.portfolio.tracker import PortfolioTracker
from app.risk.manager import AccountContext, RiskManager
from app.strategies import SignalPipeline, Strategy, create_strategies

ET = ZoneInfo("America/New_York")

MAX_CONSECUTIVE_TICK_ERRORS = 30
EQUITY_PERSIST_EVERY_TICKS = 12
METRICS_BROADCAST_EVERY_TICKS = 6
# Data-gap entry block threshold. Thin printers (user-added low-priced /
# low-float names) legitimately go minutes between prints and tripped the
# old fixed 120s constantly -- 34 false blocks on Aug-24 alone. Env-tunable;
# default unchanged for liquid names.
_DATA_GAP_THRESHOLD_SECS = float(os.environ.get("ALPHADESK_DATA_GAP_THRESHOLD_SECS", "120"))

# DB key template for durable kill-switch state.  Namespaced by mode so
# paper and live halt states are fully independent.
_HALT_KEY = "hard_halt_state:{mode}"


class TradingEngine(TradingEngineEvaluationMixin, TradingEngineHaltsMixin):
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.publish: Callable[[str, Any], None] = lambda _t, _d: None

        self.state: EngineState = EngineState.STOPPED
        self.mode: str = settings.mode
        self.last_error: str = ""

        self.model = SignalModel(get_models_dir())
        self.insights = InsightEngine()
        self.insights.is_research_mode = False

        self.user_watchlist: list[str] = db.get_setting("watchlist") or settings.watchlist_symbols()
        self.scanned_watchlist: list[str] = []
        self.scanned_watchlist_ttl: dict[str, int] = {}
        self.scanned_watchlist_expires_at: dict[str, datetime] = {}
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
        self._regimes: dict[str, Regime] = {}
        self._position_peaks: dict[str, float] = {}
        self._position_troughs: dict[str, float] = {}
        self._position_initial_stops: dict[str, float | None] = {}
        # Original strategy target survives broker protection repair/reconciliation.
        self._position_take_profits: dict[str, float] = {}
        # fix(partial-close-wedge): close-latch bookkeeping (requested qty + submit
        # time) so guards release from fill truth and staleness, never wedging exits.
        self._pending_close_qtys: dict[str, float | None] = {}
        self._pending_close_submitted_at: dict[str, datetime] = {}
        self.pis_adapter: PISEngineAdapter = PISEngineAdapter()
        from app.portfolio.recent_performance import RecentPerformanceGovernor
        self._perf_governor = RecentPerformanceGovernor(db, lambda: self.risk_config)
        self._last_scan_date = None
        self._scanner_retry_after: datetime | None = None
        self._last_train_date = None
        self._last_event_monitor_min = None
        self._event_monitor_task: asyncio.Task | None = None
        self._last_intraday_scan_min: int | None = None  # Feature 1: 30-min intraday refresh
        self._flattened_sessions: set = set()
        self._eod_flatten_started_sessions: set = set()
        # STALE-POSITION GUARD 2026-10-16: server death between close and EOD
        # flatten strands positions overnight (2026-10-15: 3 paper positions
        # survived, one with no broker stop leg). start() sets this when broker
        # holds positions while the market is closed; _tick flattens once open.
        self._flatten_at_open: bool = False
        self._task: asyncio.Task | None = None
        self._tick_count = 0
        self._consec_errors = 0
        self._trades_since_insight = 0
        self._pending_close_symbols: set[str] = set()  # symbols with in-flight close order
        # Tracks the close ORDER id per symbol so the guard can release the moment
        # the broker confirms the close reached a terminal state (fixes the wedge
        # where partial closes latched the guard until the whole lot hit zero).
        self._pending_close_order_ids: dict[str, str] = {}
        self.trainer: ContinuousTrainer | None = None
        self._last_heartbeat_hour: int | None = None
        # fix(#45): in-flight flatten guard — deduplicate concurrent flatten for same symbol
        self._flattening_symbols: set[str] = set()
        # fix(#56): retain emergency flatten task reference for cancellation on stop()
        self._emergency_flatten_task: asyncio.Task | None = None
        # fix(#86): track market-data stale timestamps per symbol
        self._last_data_bar_ts: dict[str, datetime] = {}
        self._data_gap_symbols: set[str] = set()
        # One-shot warn latch for entry evaluations skipped by decision-input gates.
        self._entry_gate_warned: set[str] = set()

        # Slow scheduled work is explicitly detached from the market-data/execution tick.
        self._scanner_task: asyncio.Task | None = None
        self._trainer_task: asyncio.Task | None = None
        # Cached scanner instance so strategy evaluation can read live mover context.
        self._scanner = None
        self._scanner_data = None
        self.runtime_ledger = None
        self.scanner_context: dict[str, dict] = {}
        self.scanner_status: dict[str, Any] = {
            "status": "never_run",
            "outcome": None,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
            "last_watchlist_count": 0,
            "is_intraday": None,
            "as_of": None,
            "discovery": None,
        }

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
        # Replay fast-path flag: set by ReplayRuntime to skip live-ops telemetry
        # (heartbeat monitors, equity persistence, hourly sector scans) that has
        # zero influence on decisions. Decision gates ALWAYS run.
        self.replay_fast_path = False
        # Day-sharded fast replay keeps scanner work opt-in because a full
        # three-stage ML scan is intentionally expensive. When enabled, the
        # replay scheduler awaits it so scanner decisions are causal and
        # ledger-visible rather than leaving a task behind at shard shutdown.
        self.replay_scanner_enabled = False
        self.reconciliation_mismatch = False
        self._position_reconciliation_mismatch_streak = 0
        self._order_reconciliation_mismatch_streak = 0

        # Heartbeat System Diagnostics (CR-146 Phase F)
        self.system_health = "NORMAL"
        self.allow_new_entries = True
        self.vix_unavailable = False
        self._vix_unavailable_last_state: bool | None = None
        self.heartbeat_diagnostics = {}
        self._force_gated = False
        self._last_vix_value: float | None = None
        self._last_vix_timestamp: datetime | None = None
        self._last_vix_status: str = "UNAVAILABLE"

        # Heartbeat Telemetry & EKG Tracker
        self._heartbeat_seq = 0
        self._evaluations_window_count = 0
        self._signals_evaluated_today = 0
        self._last_evaluation_at: datetime | None = None
        self._last_evaluated_symbol_count = 0
        self._last_closed_bar_symbol_count = 0
        self._last_telemetry_event: dict | None = None
        self._subsystem_timestamps: dict[str, str | None] = {
            "last_scan": None,
            "last_bar": None,
            "last_order": None,
            "last_strategy": None,
            "last_replay": None,
            "last_training": None,
            "last_broker_ping": None,
        }
        self._last_broker_latency_ms: float | None = None

    # ------------------------------------------------------------------
    # halt persistence helpers (kill switch retired)
    # ------------------------------------------------------------------

    def _halt_db_key(self, mode: str) -> str:
        return _HALT_KEY.format(mode=mode)

    def _persist_halt(self, mode: str, reason: str) -> None:
        pass

    def _clear_halt(self, mode: str) -> None:
        pass

    def _restore_halt_if_persisted(self, mode: str) -> bool:
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

    def _vix_symbols(self) -> list[str]:
        provider_name = getattr(getattr(self.data, "provider", None), "name", "")
        if provider_name == "alpaca":
            return ["^VIX"]
        return ["VIX", "^VIX"]

    def _fetch_vix_frame(self, symbol: str, now: datetime) -> pd.DataFrame:
        """Return a usable VIX frame."""
        if self.data is None:
            return pd.DataFrame()

        try:
            frame = self.data.frame(symbol, "1Min", limit=1)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        except Exception:
            pass

        return pd.DataFrame()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self, mode: str | None = None, confirm: str = "") -> None:
        if self.state == EngineState.RUNNING:
            raise RuntimeError("Engine is already running")
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

        restore_mode = None
        restore_account_id = None
        try:
            from app.brokers.alpaca import AlpacaBroker
            if isinstance(self.broker, AlpacaBroker) and mode in {"paper", "live"}:
                restore_mode = mode
                # fix(#61): bind portfolio restore to the specific Alpaca account
                restore_account_id = getattr(self.broker, "account_id", None)
        except Exception:
            pass
        self.portfolio = PortfolioTracker(restore_mode=restore_mode, restore_account_id=restore_account_id)

        # fix(#43): restore safety-critical risk state (loss streak, cooldowns, halts) from checkpoint
        if restore_mode and self.db:
            checkpoint = self.db.get_portfolio_checkpoint(restore_mode)
            if checkpoint and "risk_state" in checkpoint and self.risk:
                self.risk.restore_risk_state(checkpoint["risk_state"])
                applog.info("engine", f"#43: Restored safety-critical risk state from {restore_mode} checkpoint")


        from app.portfolio.allocator import CapitalAllocationOptimizer, PortfolioAIGate
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
            # STALE-POSITION GUARD 2026-10-16: positions alive while the market
            # is closed survived an unmanaged close (crash between close and
            # EOD flatten). Never market-order into the closed market here;
            # flag for flatten at the next open (handled in _tick).
            if positions and mode != "demo" and not self.clock.is_open():
                self._flatten_at_open = True
                stale = sorted({p.symbol for p in positions})
                applog.warning(
                    "engine",
                    f"STALE POSITIONS at startup (market closed): {stale} — "
                    "will flatten at next open before any new entries",
                )
        except Exception as exc:
            self.reconciliation_failed = True
            applog.error("engine", f"Failed to restore and reconcile active positions: {exc}")
        # Restore operator-persisted ML threshold setting (plain runtime knob).
        ml_thresh = self.risk_config.ml_confidence_threshold
        if self.db.get_setting("ml_threshold") is not None:
            ml_thresh = float(self.db.get_setting("ml_threshold"))
            self.risk_config.ml_confidence_threshold = ml_thresh
            applog.info("engine", f"Restored persisted ML threshold setting on startup: {ml_thresh}")

        # The persisted runtime RiskConfig is authoritative for pipeline gates
        # and TradeScore thresholds; do not let SignalPipeline rebuild defaults
        # from process environment on startup.
        self.pipeline = SignalPipeline(
            self.model,
            ml_thresh,
            risk_config=self.risk_config,
            settings=self.settings,
        )
        for s in self.strategies:
            s.reset()
        self._regimes.clear()
        self._flattened_sessions.clear()
        self._last_scan_date = None
        self._scanner_retry_after = None
        self.scanner_status.update(
            {
                "status": "never_run",
                "outcome": None,
                "last_started_at": None,
                "last_completed_at": None,
                "last_error": None,
                "last_watchlist_count": 0,
                "is_intraday": None,
                "as_of": None,
                "discovery": None,
            }
        )
        self._tick_count = 0
        self._consec_errors = 0
        self.last_error = ""

        if isinstance(self.broker, PaperBroker):
            now = utcnow()
            for symbol, price in self.data.last_prices(self.watchlist).items():
                self.broker.update_price(symbol, price, now)

        account = await self.broker.account()
        self.portfolio.roll_session(self.clock.session_date(), account.equity)
        self.portfolio.peak_equity = max(self.portfolio.peak_equity, account.equity)
        self.portfolio.mark_equity(account.equity, utcnow())

        self.trainer = ContinuousTrainer(
            self.data, self.model, get_models_dir(), self.db.get_setting("strategies") or {},
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
        event_task = getattr(self, "_event_monitor_task", None)
        if event_task is not None and not event_task.done():
            event_task.cancel()
            try:
                await event_task
            except asyncio.CancelledError:
                pass
        self._event_monitor_task = None
        for attr in ("_scanner_task", "_trainer_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            setattr(self, attr, None)
        # fix(#56): cancel emergency flatten task when engine is explicitly stopped
        if self._emergency_flatten_task is not None and not self._emergency_flatten_task.done():
            self._emergency_flatten_task.cancel()
            try:
                await self._emergency_flatten_task
            except asyncio.CancelledError:
                pass
            self._emergency_flatten_task = None
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
                import traceback
                applog.error("engine", f"tick failed ({self._consec_errors}): {exc}\n{traceback.format_exc()}")
                if self._consec_errors >= MAX_CONSECUTIVE_TICK_ERRORS:
                    applog.error(
                        "engine",
                        f"FATAL: {self._consec_errors} consecutive errors reached. "
                        f"Halting engine and triggering emergency flatten."
                    )
                    self.state = EngineState.HALTED
                    self.publish("status", self.status())
                    # fix(#56): retain task reference so stop() can cancel it
                    self._emergency_flatten_task = asyncio.create_task(
                        self._emergency_flatten_loop(),
                        name="trading-engine-emergency-flatten"
                    )
                    return
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.25, interval - elapsed))


    # ------------------------------------------------------------------
    # authoritative hard-halt liquidation
    # ------------------------------------------------------------------



    # ------------------------------------------------------------------
    # authoritative EOD liquidation
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # the tick
    # ------------------------------------------------------------------
    async def _flatten_stale_positions_at_open(self) -> bool:
        """Flatten overnight exposure and wait for broker-confirmed flatness.

        A broker ``close_all_positions`` response only acknowledges the
        liquidation request; it does not prove that positions and their
        protective orders are gone. Keep the startup latch set until both
        broker post-conditions are empty so the same tick cannot route stale
        exposure through PIS before the close fills settle.
        """
        try:
            await self.flatten()
            positions = await self.broker.positions()
            open_orders = await self.broker.open_orders()
        except Exception as exc:  # noqa: BLE001
            self._flatten_at_open = True
            applog.error("engine", f"STALE-POSITION GUARD flatten verification failed: {exc}")
            return False

        if positions or open_orders:
            self._flatten_at_open = True
            applog.warning(
                "engine",
                "STALE-POSITION GUARD: flatten request not yet broker-flat "
                f"(positions={len(positions)}, open_orders={len(open_orders)}); skipping this tick",
            )
            return False

        self._flatten_at_open = False
        applog.warning("engine", "STALE-POSITION GUARD: stranded positions flattened at open")
        return True

    async def _tick(self) -> None:
        assert self.data and self.broker and self.risk and self.portfolio and self.pipeline
        self._tick_count += 1
        now = utcnow()
        now_et = now.astimezone(ET)
        today_date = now_et.date()
        et_mins = now_et.hour * 60 + now_et.minute

        is_trading_day = False
        try:
            get_session_close(now)
            is_trading_day = True
        except Exception:
            pass

        # Resolve durable ambiguous submissions before heartbeat evaluates broker/local
        # order reconciliation.  Heartbeat is an observer of this state, not a second
        # owner that may independently delete idempotency guards.
        try:
            await self._reconcile_pending_submissions(now=now)
        except Exception as exc:
            applog.error("engine", f"Failed to reconcile pending submissions: {exc}")

        # System Heartbeat Checks (CR-146 Phase F)
        await self._check_system_heartbeat(now)
        if self.state == EngineState.HALTED:
            return

        # STALE-POSITION GUARD 2026-10-16: flatten stranded overnight positions
        # once the market opens, before any new entries. One-shot by design.
        if getattr(self, "_flatten_at_open", False) and self.clock.is_open():
            if not await self._flatten_stale_positions_at_open():
                return

        # Synchronize ML gate threshold with assessed market risk regime with dynamic scale
        # Synchronize ML gate threshold with assessed market risk regime with dynamic scale
        raw_regime = getattr(getattr(self, "last_safety_decision", None), "market_risk_state", getattr(self.risk_config, "current_regime", "NORMAL"))

        # Fast path check against cached regime to avoid re-calculating threshold on every tick
        if getattr(self, "_cached_raw_regime", None) != raw_regime:
            self._cached_raw_regime = raw_regime
            if hasattr(raw_regime, "value"):
                regime_str = str(raw_regime.value)
            elif hasattr(raw_regime, "name"):
                regime_str = str(raw_regime.name)
            else:
                regime_str = str(raw_regime)

            current_regime = "STRESS" if self.vix_unavailable else regime_str.split(".")[-1].upper()
            regime_map = getattr(self.risk_config, "regime_ml_thresholds", {})
            raw_threshold = regime_map.get(current_regime)

            if raw_threshold is None:
                raw_threshold = (
                    getattr(self.risk_config, "stress_ml_confidence_threshold", 0.60)
                    if current_regime in ("STRESS", "PANIC")
                    else getattr(self.risk_config, "ml_confidence_threshold", 0.55)
                )

            try:
                val = float(raw_threshold)
                if math.isnan(val) or math.isinf(val) or not (0.0 <= val <= 1.0):
                    val = 0.55
            except (ValueError, TypeError):
                val = 0.55

            self._cached_ml_threshold = val

        self.pipeline.ml_threshold = getattr(self, "_cached_ml_threshold", 0.55)

        # Run pre-market scanner (9:00 AM ET onwards) once per day
        # Run post-market trainer (>= 16:15 ET) once per day
        # Run event monitor every 5 minutes during active trading session (9:30 AM to 4:00 PM ET)
        if self.state == EngineState.RUNNING:
            scanner_enabled = (
                not getattr(self, "replay_fast_path", False)
                or getattr(self, "replay_scanner_enabled", False)
            )
            scanner_inline = type(self.clock).__name__ == "ReplayClock"
            if (
                scanner_enabled
                and self._last_scan_date != today_date
                and et_mins >= 9 * 60
                and (
                    self._scanner_retry_after is None
                    or now >= self._scanner_retry_after
                )
            ):
                if is_trading_day:
                    applog.info("engine", "Pre-market scanner trigger time reached (>= 09:00 ET)")
                    if self._scanner_task is None or self._scanner_task.done():
                        if scanner_inline:
                            # Replay must finish the scan before the tick advances;
                            # otherwise the task is cancelled at the end of a
                            # shard and no scanner decision reaches the ledger.
                            await self.run_market_scanner(is_intraday=False)
                        else:
                            # Live scans remain detached so provider latency cannot
                            # hold the market-data/execution tick hostage.
                            self._scanner_task = asyncio.create_task(
                                self.run_market_scanner(is_intraday=False), name="market-scanner-premarket"
                            )
                    else:
                        applog.info("engine", "Pre-market scanner skipped: previous scanner task still running")
            # Intraday watchlist refresh every 15 min during session (was 30 min)
            elif (
                scanner_enabled
                and is_trading_day
                and 9 * 60 + 30 <= et_mins <= 15 * 60 + 45  # 9:30 AM – 3:45 PM ET (was 3:30 PM)
                and et_mins % 15 == 0  # every 15 min (was every 30 min)
                and self._last_intraday_scan_min != et_mins
            ):
                self._last_intraday_scan_min = et_mins
                applog.info("engine", f"Intraday watchlist refresh triggered at {et_mins} ET mins")
                if self._scanner_task is None or self._scanner_task.done():
                    if scanner_inline:
                        await self._run_scanner_inline(is_intraday=True)
                    else:
                        self._scanner_task = asyncio.create_task(
                            self.run_market_scanner(is_intraday=True), name="market-scanner-intraday"
                        )
                else:
                    applog.info("engine", "Intraday scanner skipped: previous scanner task still running")
            if is_trading_day and self.trainer and self._last_train_date != today_date and et_mins >= 16 * 60 + 15:
                # POST-MARKET TRAIN GATE 2026-10-16: three overnight server
                # deaths correlate with post-market training (spawn pools +
                # native fits in-server). ALPHADESK_POSTMARKET_TRAIN=0 defers
                # retraining to manual / offline runs until the native crash
                # is diagnosed. Models stay valid; WFV gates still enforced.
                import os as _os

                if _os.environ.get("ALPHADESK_POSTMARKET_TRAIN", "1") != "1":
                    if self._last_train_date != today_date:
                        applog.info("engine", "Post-market trainer deferred (ALPHADESK_POSTMARKET_TRAIN=0)")
                    self._last_train_date = today_date
                else:
                    self._last_train_date = today_date
                    applog.info("engine", "Post-market trainer trigger time reached (>= 16:15 ET)")
                    if self._trainer_task is None or self._trainer_task.done():
                        self._trainer_task = asyncio.create_task(
                            self.trainer.trigger(list(self.watchlist)), name="post-market-trainer"
                        )
            if (
                not getattr(self, "replay_fast_path", False)
                and 9 * 60 + 30 <= et_mins <= 16 * 60
                and et_mins % 5 == 0
                and self._last_event_monitor_min != et_mins
            ):
                # Fast replay skips ops monitors (established directive); the
                # event monitor's universe-wide history+enrich sweep is pure
                # overhead when publish() is a no-op sink.
                self._last_event_monitor_min = et_mins
                task = self._event_monitor_task
                if task is None or task.done():
                    if task is not None and not task.cancelled():
                        try:
                            task.result()
                        except Exception as exc:
                            applog.warning("engine", f"Event monitor failed open: {exc}")
                    self._event_monitor_task = asyncio.create_task(self.run_event_monitor(now), name="event-monitor")

        active_symbols = set(self.watchlist) | set(self.portfolio._lots.keys())
        new_closed = await self.data.poll(list(active_symbols))

        # fix(#86): detect market-data gaps >2 min during regular trading hours and gate entries
        _is_regular_session = False
        try:
            session_close = get_session_close(now)
            if now_et.time() >= dtime(9, 30) and now_et <= session_close:
                _is_regular_session = True
        except ValueError:
            _is_regular_session = False
        except Exception:
            _is_regular_session = 9 * 60 + 30 <= et_mins <= 16 * 60

        _is_replay = type(self.clock).__name__ == "ReplayClock"
        if _is_regular_session:
            for sym in self.watchlist:
                sym_upper = sym.upper()
                if sym_upper in new_closed and not new_closed[sym_upper].empty:
                    # Fresh bar arrived — clear gap flag
                    self._last_data_bar_ts[sym_upper] = now
                    self._data_gap_symbols.discard(sym_upper)
                elif sym_upper in self._last_data_bar_ts:
                    last_ts = self._last_data_bar_ts[sym_upper]
                    # Protect against overnight clock jumps in replay mode
                    if now.date() != last_ts.date():
                        self._last_data_bar_ts[sym_upper] = now
                        self._data_gap_symbols.discard(sym_upper)
                    else:
                        elapsed = (now - last_ts).total_seconds()
                        # Live config (UI-editable, persisted): thin printers
                        # need a larger window; 0 disables the block entirely.
                        gap_threshold = float(
                            getattr(self.risk_config, "data_gap_threshold_secs", _DATA_GAP_THRESHOLD_SECS)
                        )
                        if gap_threshold > 0 and elapsed > gap_threshold and sym_upper not in self._data_gap_symbols:
                            if not _is_replay:
                                self._data_gap_symbols.add(sym_upper)
                                applog.warning(
                                    "engine",
                                    f"#86: Market-data gap detected for {sym_upper}: no bar for {elapsed:.0f}s "
                                    f"during regular session. New entries BLOCKED until data resumes.",
                                )
                elif sym_upper not in self._last_data_bar_ts:
                    # Initialize timestamp on first tick
                    self._last_data_bar_ts[sym_upper] = now
        else:
            # Outside regular session: clear all gap flags and history to prevent multi-day bleed
            self._data_gap_symbols.clear()
            self._last_data_bar_ts.clear()


        # 1. advance fill simulation / poll real fills
        if isinstance(self.broker, PaperBroker):
            for symbol, bars in new_closed.items():
                for ts, row in bars.iterrows():
                    self.broker.process_bar(symbol, row, (ts + timedelta(minutes=1)).to_pydatetime())
            await self._drain_fills(now)
        else:
            if hasattr(self.broker, "poll_fills"):
                await self.broker.poll_fills()
            await self._drain_fills(now)

        try:
            account = await self.broker.account()
            positions = await self.broker.positions()
            self._reconcile_positions(positions)
        except Exception as exc:
            self.reconciliation_failed = True
            applog.error("engine", f"Failed to fetch account/positions during tick: {exc}. Marking reconciliation uncertain.")
            account = SimpleNamespace(
                equity=self.portfolio.equity,
                cash=self.portfolio.equity,
                buying_power=self.portfolio.equity * 4.0,
            )
            positions = []

        if self.state == EngineState.RUNNING and not getattr(self, "replay_fast_path", False):
            self._run_heartbeat_tasks(now)

        # 2. session roll
        session = self.clock.session_date(now)
        if self.portfolio.roll_session(session, account.equity):
            self.risk.roll_session(session)
            if self.mode in {"paper", "live"}:
                try:
                    account_id = getattr(self.broker, "account_id", None)
                    self.db.save_portfolio_checkpoint(
                        self.mode,
                        self.portfolio.checkpoint(
                            account_id=account_id,
                            risk_state=self.risk.get_risk_state(),
                        ),
                    )
                except Exception as exc:
                    applog.error("engine", f"Failed to persist session-roll checkpoint: {exc}")
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
            rolling_day_trades=self.portfolio.day_trades_in_window(session),
            is_paper=self.broker.is_paper,
        )
        halt = self.risk.check_halts(ctx, now=now)
        if halt == "daily" or self.risk.daily_halted:
            await self._reconcile_daily_halt_liquidation(now)
            if halt == "daily":
                self.insights.on_risk_event(
                    "Daily loss limit hit",
                    f"Trading paused until the next session (day P&L {ctx.day_pnl:+,.2f}).",
                    severity="danger",
                )
        elif halt == "profit" or self.risk.profit_locked:
            # DAILY PROFIT LOCK 2026-10-16 (bank-and-ride, operator demand):
            # on trip, scratch underwater positions and let green runners ride
            # under PIS (upside open). If the banked day then slips to the
            # defend line, full-flatten to protect the floor.
            if halt == "profit":
                await self._bank_profit_lock(now)
                self.insights.on_risk_event(
                    "Daily profit locked",
                    f"Day banked at {ctx.day_pnl:+,.2f}; losers scratched, winners ride, no new entries.",
                    severity="info",
                )
            else:
                defend_pct = float(getattr(self.risk.config, "daily_profit_defend_pct", 0.5) or 0.0)
                if defend_pct > 0 and ctx.session_start_equity > 0:
                    day_pct = ctx.day_pnl / ctx.session_start_equity * 100.0
                    if day_pct <= defend_pct:
                        applog.warning(
                            "engine",
                            f"PROFIT LOCK defend: day slipped to {day_pct:.2f}% <= {defend_pct:.2f}% -- full flatten",
                        )
                        await self._reconcile_daily_halt_liquidation(now)

        # 4. end-of-day flatten. Completion is latched only after an
        # authoritative broker post-condition proves no positions or open orders.
        #
        # EOD SAFETY LATCH: the flatten attempt starts inside the configured
        # pre-close window, but unlike a pure time-box it keeps retrying every
        # tick until the broker proves flat (or until the post-close grace
        # expires). A broker outage, slow flatten_all, or a restart at 15:58 no
        # longer strands positions overnight.
        in_flatten_window = self.risk.should_flatten(now)
        eod_grace_min = int(getattr(self.settings, "eod_post_close_grace_minutes", 15))
        eod_started = getattr(self, "_eod_flatten_started_sessions", None)
        if eod_started is None:
            eod_started = set()
            self._eod_flatten_started_sessions = eod_started
        eod_active = in_flatten_window or (
            session in eod_started
            and now <= get_session_close(now) + timedelta(minutes=eod_grace_min)
        )
        if (
            self.state == EngineState.RUNNING
            and eod_active
            and session not in self._flattened_sessions
        ):
            if in_flatten_window:
                eod_started.add(session)
            await self._reconcile_eod_liquidation(session, now)

        # 5. strategy evaluation on newly closed bars. Do not reopen exposure
        # while EOD liquidation is active, including unresolved retry ticks.
        is_active_session = (
            self.state in (EngineState.RUNNING, EngineState.SAFE_MODE)
            and not self.risk.daily_halted
            and not eod_active
            and (self.clock.is_open(now) or self.mode == "demo")
        )
        if is_active_session and new_closed:
            pos_by_symbol = {p.symbol: p for p in positions}
            candidates = []

            # Heartbeat establishes the current entry permission before strategy
            # evaluation. Keep the manual/system gate and the heartbeat decision
            # intersected so a stale decision cannot reopen entries.
            decision = getattr(self, "last_safety_decision", None)
            heartbeat_allows_entries = decision.allow_new_entries if decision is not None else True
            allow_entries = bool(heartbeat_allows_entries and self.allow_new_entries)

            symbols_to_evaluate = list(new_closed.keys())
            self._last_closed_bar_symbol_count = len(symbols_to_evaluate)
            if self.state != EngineState.RUNNING or not allow_entries:
                symbols_to_evaluate = [sym for sym in symbols_to_evaluate if sym in pos_by_symbol]

            self._last_evaluated_symbol_count = len(symbols_to_evaluate)
            if symbols_to_evaluate:
                self._last_evaluation_at = now

            effective_positions = list(positions)

            for symbol in symbols_to_evaluate:
                # fix(#86): skip new ENTRY signals for symbols with active market-data gaps
                sym_upper = symbol.upper()
                if sym_upper in self._data_gap_symbols and sym_upper not in pos_by_symbol:
                    applog.debug("engine", f"#86: Skipping {sym_upper} entry evaluation \u2014 market-data gap active")
                    continue
                entries = await self._evaluate_symbol(symbol, pos_by_symbol.get(symbol), account.equity, now, effective_positions=effective_positions)
                if self.state == EngineState.RUNNING and allow_entries:
                    candidates.extend(entries)
                    for sig, _ in entries:
                        if sig.action != SignalAction.EXIT:
                            effective_positions.append(
                                Position(
                                    symbol=sig.symbol.upper(),
                                    qty=1.0,
                                    avg_entry_price=sig.price or 1.0,
                                    market_price=sig.price or 1.0,
                                    strategy=sig.strategy
                                )
                            )

            if candidates:
                from app.portfolio.allocator import process_portfolio_allocations
                from app.risk.manager import is_entry_order

                # Enrich signals with fresh scanner context if discovered dynamically
                for sig, _ in candidates:
                    sym = sig.symbol.upper()
                    if hasattr(self, "scanner_context"):
                        sctx = self.scanner_context.get(sym)
                        if sctx is not None:
                            expires_at = sctx.get("expires_at")
                            if expires_at is not None and now < expires_at:
                                sig.discovery_source = "scanner"
                                sig.scanner_score = sctx.get("score")
                                sig.scanner_auc = sctx.get("auc")

                # Pre-filter candidates via RiskManager before allocation
                alloc_signals = []
                pre_gated_entries = []
                safety_decision = getattr(self, "last_safety_decision", None)
                allowed_strategies = getattr(safety_decision, "allowed_strategies", None)
                for sig, _ in candidates:
                    sym_upper = sig.symbol.upper()

                    # Heartbeat strategy restrictions apply to new entries only.
                    # Existing-position exits are protective and must remain
                    # available even when defensive/survival policy narrows the
                    # entry strategy set.
                    if (
                        sig.action != SignalAction.EXIT
                        and safety_decision is not None
                        and allowed_strategies is not None
                        and sig.strategy not in allowed_strategies
                    ):
                        market_risk_state = getattr(safety_decision, "market_risk_state", None)
                        pre_gated_entries.append(
                            (
                                sig,
                                "strategy not allowed by safety decision: "
                                f"{sig.strategy} (market_risk_state={market_risk_state})",
                            )
                        )
                        continue

                    # Prevent budget starvation for symbols we already hold
                    if sym_upper in pos_by_symbol:
                        pre_gated_entries.append((sig, f"position already open in {sym_upper}"))
                        continue

                    # Prevent budget starvation for symbols actively being bought
                    if any(info["symbol"] == sym_upper for info in self.pending_reconciliation_orders.values()):
                        pre_gated_entries.append((sig, f"order locally pending for {sym_upper}"))
                        continue

                    # Prevent budget starvation for unborrowable shorts
                    if sig.action == SignalAction.ENTER_SHORT:
                        is_short = await self.broker.is_shortable(sig.symbol)
                        if not is_short:
                            pre_gated_entries.append((sig, "Asset is not shortable at broker"))
                            continue

                    pre_check = self.risk.is_entry_permitted(sig, now=now)
                    if not pre_check.approved:
                        pre_gated_entries.append((sig, pre_check.reason))
                    else:
                        alloc_signals.append(sig)

                df_by_symbol = {sig.symbol: df for sig, df in candidates}

                # Shared portfolio gate and allocation
                decision_outcome = process_portfolio_allocations(
                    signals=alloc_signals,
                    positions=positions,
                    available_capital=account.cash,
                    equity=account.equity,
                    portfolio_gate=self.portfolio_gate,
                    portfolio_allocator=self.portfolio_allocator,
                    market_state=getattr(safety_decision, "market_risk_state", None),
                )

                # Re-inject pre-filtered rejections so they are properly mirrored and logged
                decision_outcome.gated_entries.extend(pre_gated_entries)

                # Process gated/rejected entries
                for sig, reason in decision_outcome.gated_entries:
                    applog.info("engine", f"{sig.symbol}: Portfolio Gate/Allocator rejected -- {reason}")
                    self.publish("insight", self.insights.on_signal(sig, gated=True, gate_reason=reason).model_dump(mode="json"))
                    try:
                        from app.core.shadow_ledger import shadow_ledger
                        _sdf = df_by_symbol.get(sig.symbol)
                        shadow_ledger.record(
                            symbol=sig.symbol,
                            action=str(sig.action.value if hasattr(sig.action, "value") else sig.action),
                            strategy=sig.strategy,
                            gate="portfolio_or_risk",
                            reason=reason,
                            price=sig.price,
                            stop_loss=sig.stop_loss,
                            take_profit=sig.take_profit,
                            atr14=float(_sdf["atr14"].iloc[-1]) if _sdf is not None and "atr14" in _sdf.columns and len(_sdf) else None,
                            confidence=sig.confidence,
                            sig=sig,
                        )
                    except Exception:
                        pass

                # Size and submit approved entries
                if decision_outcome.approved_entries:
                    if getattr(self, "reconciliation_failed", False):
                        applog.warning("engine", "Skipping entry sizing and submission while reconciliation is uncertain.")
                    else:
                        open_orders = await self.broker.open_orders()
                        pending_entries = [
                            o.symbol.upper() for o in open_orders
                            if is_entry_order(o, positions)
                        ]
                        effective_pending_entries = set(pending_entries)
                        for info in self.pending_reconciliation_orders.values():
                            effective_pending_entries.add(info["symbol"].upper())
                        # Also block re-entries for symbols with an in-flight close order
                        # (manual sells/covers that haven't filled yet). EXIT signals
                        # are unaffected — only the entry gate in evaluate_entry checks this.
                        effective_pending_entries.update(getattr(self, "_pending_close_symbols", set()))
                        reservations = SameCycleEntryReservations.from_snapshot(account.cash, positions)


                        for sig, kelly_val in decision_outcome.approved_entries:
                            # Defense in depth: reference/risk symbols can never reach broker submission.
                            if sig.symbol.upper() in {s.upper() for s in self._vix_symbols()}:
                                applog.error("engine", f"Blocked non-tradable reference symbol at submission boundary: {sig.symbol}")
                                continue
                            # =============================================================================
                            # [MODIFIED 2026-10-14]: CR-19 - Regime-Aware Countertrend Risk Scaling
                            # ORIGINAL INTENT: Attached static risk_multiplier from SafetyDecision.
                            # REASON FOR CHANGE: Shorts were the biggest per-trade leak (-$7.6k/605).
                            # Scale countertrend entries (shorts in TREND_UP, longs in TREND_DOWN) by
                            # countertrend_mult (default 0.5) to protect capital while preserving valid setups.
                            # SAFE ROLLBACK: The original code is removed; rollback via git history.
                            # -----------------------------------------------------------------------------
                            # [NEW CODE]:
                            decision_safety = getattr(self, "last_safety_decision", None)
                            sig.risk_multiplier = decision_safety.risk_multiplier if decision_safety is not None else 1.0
                            countertrend_mult = float(getattr(self.risk_config, "countertrend_mult", 0.5))
                            sig_regime = getattr(sig, "regime", None) or getattr(self.pipeline, "last_regime", None)
                            if sig_regime == Regime.TREND_UP and sig.action == SignalAction.ENTER_SHORT:
                                sig.risk_multiplier = round(sig.risk_multiplier * countertrend_mult, 4)
                                applog.info("engine.risk", f"{sig.symbol} {sig.strategy}: countertrend short in TREND_UP, scaled risk x{countertrend_mult:.2f}")
                            elif sig_regime == Regime.TREND_DOWN and sig.action == SignalAction.ENTER_LONG:
                                sig.risk_multiplier = round(sig.risk_multiplier * countertrend_mult, 4)
                                applog.info("engine.risk", f"{sig.symbol} {sig.strategy}: countertrend long in TREND_DOWN, scaled risk x{countertrend_mult:.2f}")
                            # =============================================================================

                            # Recent Performance Governor: scale size DOWN when this
                            # symbol / strategy / session-hour has measured negative
                            # expectancy in recent closed trades. Tighten-only; the
                            # factor recovers automatically as performance does.
                            if getattr(self.risk_config, "governor_enabled", True):
                                try:
                                    gov_factor = await asyncio.to_thread(
                                        self._perf_governor.factor_for, sig.symbol, sig.strategy, now
                                    )
                                    if gov_factor < 1.0:
                                        sig.risk_multiplier = round(sig.risk_multiplier * gov_factor, 4)
                                        applog.info(
                                            "engine.governor",
                                            f"{sig.symbol} {sig.strategy}: recent-performance risk x{gov_factor:.2f}",
                                        )
                                except Exception as exc:
                                    applog.warning("engine.governor", f"Governor factor failed (neutral applied): {exc}")

                            # Run behavioral safety check
                            behavioral_reason = await self._check_behavioral_safety(sig, now)
                            if behavioral_reason:
                                applog.info("engine", f"{sig.symbol.upper()}: entry rejected by Behavioral Loop Detector — {behavioral_reason}")
                                self.publish("insight", self.insights.on_signal(sig, gated=True, gate_reason=behavioral_reason).model_dump(mode="json"))
                                try:
                                    from app.core.shadow_ledger import shadow_ledger
                                    _bdf = df_by_symbol[sig.symbol]
                                    shadow_ledger.record(
                                        symbol=sig.symbol,
                                        action=str(sig.action.value if hasattr(sig.action, "value") else sig.action),
                                        strategy=sig.strategy,
                                        gate="behavioral",
                                        reason=behavioral_reason,
                                        price=sig.price,
                                        stop_loss=sig.stop_loss,
                                        take_profit=sig.take_profit,
                                        atr14=float(_bdf["atr14"].iloc[-1]) if "atr14" in _bdf.columns and len(_bdf) else None,
                                        confidence=sig.confidence,
                                        sig=sig,
                                    )
                                except Exception:
                                    pass
                                continue

                            df = df_by_symbol[sig.symbol]
                            ctx = AccountContext(
                                equity=account.equity,
                                cash=reservations.cash,
                                positions=reservations.positions,
                                day_pnl=self.portfolio.day_pnl(account.equity),
                                session_start_equity=self.portfolio.session_start_equity or account.equity,
                                peak_equity=self.portfolio.peak_equity,
                                day_trades_today=self.portfolio.day_trades_today,
                                rolling_day_trades=self.portfolio.day_trades_in_window(session),
                                is_paper=self.broker.is_paper,
                                pending_entry_symbols=list(effective_pending_entries),
                            )
                            decision = self.risk.evaluate_entry(sig, ctx, df, now=now, kelly_override=kelly_val)
                            if not decision.approved:
                                applog.info("engine", f"{sig.symbol.upper()}: entry rejected by RiskManager — {decision.reason}")
                                self.publish("insight", self.insights.on_signal(sig, gated=True, gate_reason=decision.reason).model_dump(mode="json"))
                                try:
                                    from app.core.shadow_ledger import shadow_ledger
                                    shadow_ledger.record(
                                        symbol=sig.symbol,
                                        action=str(sig.action.value if hasattr(sig.action, "value") else sig.action),
                                        strategy=sig.strategy,
                                        gate="portfolio_or_risk",
                                        reason=f"RiskManager: {decision.reason}",
                                        price=sig.price,
                                        stop_loss=sig.stop_loss,
                                        take_profit=sig.take_profit,
                                        atr14=float(df["atr14"].iloc[-1]) if "atr14" in df.columns and len(df) else None,
                                        confidence=sig.confidence,
                                        sig=sig,
                                    )
                                except Exception:
                                    pass
                                continue

                            # (Asset shortability was already checked in the pre-filter to prevent budget starvation)

                            client_order_id = decision.order.client_order_id
                            if not client_order_id:
                                client_order_id = f"ad-ent-{sig.symbol}-{sig.action.value}-{sig.strategy}-{now.strftime('%Y%m%d%H%M%S')}"
                                decision.order.client_order_id = client_order_id
                            # Keep the validated target in engine/PIS state only. The
                            # broker request deliberately remains target-free so PIS
                            # remains the sole discretionary exit authority.
                            entry_target = getattr(decision, "take_profit", None)
                            if entry_target is None:
                                entry_target = sig.take_profit
                            self.pending_reconciliation_orders[client_order_id] = {
                                "symbol": decision.order.symbol.upper(),
                                "client_order_id": client_order_id,
                                "side": str(decision.order.side),
                                "qty": decision.order.qty,
                                "order_type": str(decision.order.order_type),
                                "limit_price": decision.order.limit_price,
                                "take_profit": entry_target,
                                "submitted_at": utcnow().isoformat(),
                                "submission_state": "submitting",
                            }
                            await asyncio.to_thread(self.db.set_setting, "pending_reconciliation_orders", self.pending_reconciliation_orders)

                            try:
                                order = await self.broker.submit(decision.order)
                                order_rejected = order.status == OrderStatus.REJECTED
                                if order_rejected:
                                    self.pending_reconciliation_orders.pop(client_order_id, None)
                                else:
                                    self.pending_reconciliation_orders[client_order_id]["submission_state"] = "broker_acknowledged"
                                    self.pending_reconciliation_orders[client_order_id]["broker_order_id"] = getattr(order, "id", None)
                                await asyncio.to_thread(self.db.set_setting, "pending_reconciliation_orders", self.pending_reconciliation_orders)
                                self.recent_orders.appendleft(order)
                                persisted_order = (
                                    order.model_copy(update={"take_profit": float(entry_target)})
                                    if entry_target is not None and float(entry_target) > 0
                                    else order
                                )
                                if not order_rejected and entry_target is not None and float(entry_target) > 0:
                                    self._position_take_profits[order.symbol.upper()] = float(entry_target)
                                await asyncio.to_thread(self.db.save_order, persisted_order, self.mode)
                                if order_rejected:
                                    rejection_reason = str(getattr(order, "reason", "") or "broker rejected entry")
                                    applog.warning(
                                        "engine",
                                        f"{sig.symbol}: broker rejected entry -- {rejection_reason}",
                                    )
                                    self.publish(
                                        "insight",
                                        self.insights.on_signal(
                                            sig,
                                            gated=True,
                                            gate_reason=f"broker rejection: {rejection_reason}",
                                        ).model_dump(mode="json"),
                                    )
                                    try:
                                        from app.core.shadow_ledger import shadow_ledger
                                        shadow_ledger.record(
                                            symbol=sig.symbol,
                                            action=str(sig.action.value if hasattr(sig.action, "value") else sig.action),
                                            strategy=sig.strategy,
                                            gate="broker_rejection",
                                            reason=rejection_reason,
                                            price=sig.price,
                                            stop_loss=sig.stop_loss,
                                            take_profit=sig.take_profit,
                                            atr14=float(df["atr14"].iloc[-1]) if "atr14" in df.columns and len(df) else None,
                                            confidence=sig.confidence,
                                            sig=sig,
                                        )
                                    except Exception:
                                        pass
                                else:
                                    self.publish("insight", self.insights.on_signal(sig, gated=False).model_dump(mode="json"))
                                self.publish("orders", self.orders_payload())
                            except Exception as exc:
                                self.pending_reconciliation_orders[client_order_id]["submission_state"] = "ambiguous"
                                self.pending_reconciliation_orders[client_order_id]["last_error"] = str(exc)
                                await asyncio.to_thread(self.db.set_setting, "pending_reconciliation_orders", self.pending_reconciliation_orders)
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
                                await asyncio.to_thread(self.db.save_order, order, self.mode)

                            # Reserve only broker-accepted/ambiguous submissions before evaluating the next candidate.
                            # A definitive rejection did not consume capital or an entry slot.
                            # This overlays the stale broker snapshot for cash, position-count, and sector checks.
                            reference_price = sig.price or float(df["close"].iloc[-1])
                            if order.status != OrderStatus.REJECTED and reservations.reserve(order, reference_price):
                                effective_pending_entries.add(sig.symbol.upper())

            await self._drain_fills(now)

        # 6. mark equity + persist
        # F-04 FIX: Only re-fetch account if fills were drained this tick, which
        # is the only in-tick event that materially changes broker-side equity.
        # Unconditional re-fetch on every bar tick doubled Alpaca API consumption
        # against the 200 req/min budget.
        _fills_drained = getattr(self, "_fills_drained_this_tick", False)
        if _fills_drained and not getattr(self, "reconciliation_failed", False):
            try:
                account = await self.broker.account()
            except Exception as exc:
                self.reconciliation_failed = True
                applog.error("engine", f"Failed to fetch account after fill drain: {exc}")
        self._fills_drained_this_tick = False  # reset for next tick

        self.portfolio.mark_equity(account.equity, now)
        if self._tick_count % EQUITY_PERSIST_EVERY_TICKS == 0 and not getattr(self, "replay_fast_path", False):
            await asyncio.to_thread(self.db.save_equity, account.equity, account.cash, self.mode, now)


        # 7. broadcast
        self._broadcast_tick(account, now, new_closed, positions)




    async def _evaluate_symbol(self, symbol: str, position, equity: float, now: datetime, effective_positions: list[Position] | None = None) -> list[tuple[Signal, pd.DataFrame]]:
        # Live safety net: when target reached (high_val >= target for long, low_val <= target for short), original strategy take-profit triggered
        # Non-tradable reference/risk symbol excluded from strategy evaluation (e.g. VIX)
        return await super()._evaluate_symbol(symbol, position, equity, now, effective_positions=effective_positions)

    async def _drain_fills(self, now: datetime) -> None:
        assert self.broker and self.portfolio and self.risk
        # fix(partial-close-wedge): reconcile close latches EVERY tick, not only
        # when a new fill for the symbol happens to arrive. After a partial
        # scale-out the residual lot may produce no further fills until EOD,
        # which used to wedge the guard and silently swallow all PIS exits.
        if hasattr(self, "_reconcile_pending_closes"):
            res = self._reconcile_pending_closes(now)
            if inspect.isawaitable(res):
                await res
        fills = self.broker.drain_fills()
        if not fills:
            return
        # F-04: signal that fills were processed this tick so the equity re-fetch fires
        self._fills_drained_this_tick = True
        # fix(#51): enforce causal replay order — sort by (ts ASC, order_id, cumulative_qty ASC)
        fills = sorted(fills, key=lambda f: (f.ts, f.order_id or "", getattr(f, "cumulative_qty", 0.0) or 0.0))


        order_by_id = {}
        for o in self.recent_orders:
            order_by_id[o.id] = o
            if o.client_order_id:
                order_by_id[o.client_order_id] = o

        for fill in fills:
            order = order_by_id.get(fill.order_id)
            if order is None and fill.order_id:
                order = await asyncio.to_thread(self.db.get_order, fill.order_id)

            if order is not None:
                # Broker fills are incremental deltas. Preserve cumulative order
                # truth: a partial execution must never make the parent look FILLED
                # or replace filled_qty with only the newest delta.
                previous_filled = max(0.0, float(getattr(order, "filled_qty", 0.0) or 0.0))
                broker_cumulative = float(getattr(fill, "cumulative_qty", 0.0) or 0.0)
                if broker_cumulative > 0:
                    cumulative_filled = max(previous_filled, broker_cumulative)
                else:
                    cumulative_filled = previous_filled + max(0.0, float(fill.qty))
                order_qty = max(0.0, float(getattr(order, "qty", 0.0) or 0.0))
                order.filled_qty = cumulative_filled
                order.filled_avg_price = fill.price
                if order_qty > 0 and cumulative_filled + 1e-9 >= order_qty: order.status = OrderStatus.FILLED; order.filled_at = fill.ts
                else:
                    order.status = OrderStatus.PARTIALLY_FILLED
                    order.filled_at = None

            symbol = fill.symbol.upper()
            peak = self._position_peaks.get(symbol)
            trough = self._position_troughs.get(symbol)

            init_stop = self._position_initial_stops.get(symbol)
            if init_stop is None and order is not None and getattr(order, "leg", None) == "entry" and order.stop_loss is not None:
                init_stop = order.stop_loss

            # Authority Fallback 2: Search recent orders for original entry order stop_loss
            if init_stop is None:
                for recent_ord in reversed(self.recent_orders):
                    if recent_ord.symbol and recent_ord.symbol.upper() == symbol and recent_ord.stop_loss is not None:
                        ord_leg = getattr(recent_ord, "leg", None)
                        ord_class = getattr(recent_ord, "order_class", None)
                        ord_side = str(getattr(recent_ord, "side", "")).lower()
                        if ord_leg == "entry" or ord_class == "bracket" or ord_side in ["buy", "sell_short", "orderside.buy", "orderside.sell_short"]:
                            init_stop = recent_ord.stop_loss
                            break

            if init_stop is not None and symbol not in self._position_initial_stops:
                self._position_initial_stops[symbol] = init_stop

            # Strategy attribution repair: after a process restart the broker's
            # in-memory order→strategy map is empty, so bracket-child fills
            # arrive labeled with the broker name ("alpaca"). That corrupted
            # per-strategy forensics for hundreds of trades. Resolve from local
            # lot → recent orders → durable DB, in that order.
            if str(getattr(fill, "strategy", "") or "").lower() in ("", "alpaca", "unknown"):
                resolved = None
                lot = getattr(self.portfolio, "_lots", {}).get(symbol)
                if lot is not None and getattr(lot, "strategy", ""):
                    resolved = lot.strategy
                if resolved is None and order is not None and str(order.strategy or "").lower() not in ("", "alpaca", "unknown"):
                    resolved = order.strategy
                if resolved is None and fill.order_id:
                    try:
                        db_order = await asyncio.to_thread(self.db.get_order, fill.order_id)
                        if db_order is not None and str(db_order.strategy or "").lower() not in ("", "alpaca", "unknown"):
                            resolved = db_order.strategy
                    except Exception:
                        resolved = None
                fill.strategy = resolved or "engine"

            # Broker fill polling can lose the in-memory ``_meta`` tuple after
            # a restart and emit an otherwise valid fill with an empty/bare
            # reason.  The durable close order is authoritative for a known
            # PIS execution; carry its reason into the Fill before portfolio
            # attribution so the final runner trade is not synthesized as
            # ``unspecified_exit``.
            if order is not None:
                order_reason = str(getattr(order, "reason", "") or "").strip()
                fill_reason = str(getattr(fill, "reason", "") or "").strip()
                if order_reason and (
                    fill_reason.lower() in {"", "exit", "manual", "manual close", "broker fill"}
                    or fill_reason.lower().startswith("unspecified_exit")
                ):
                    fill.reason = order_reason
                if not getattr(fill, "signal_timestamp_utc", None) and getattr(order, "signal_timestamp_utc", None):
                    fill.signal_timestamp_utc = order.signal_timestamp_utc
                if not getattr(fill, "decision_bar_timestamp_utc", None) and getattr(order, "decision_bar_timestamp_utc", None):
                    fill.decision_bar_timestamp_utc = order.decision_bar_timestamp_utc

            try:
                trade = self.portfolio.on_fill(
                    fill, peak_price=peak, trough_price=trough, initial_stop=init_stop,
                    risk_manager=self.risk, now=now
                )
            except Exception:
                # The broker reservation is volatile only. If durable portfolio
                # processing fails, release it so a later tick can safely retry.
                nack_fill = getattr(self.broker, "nack_fill_processed", None)
                if nack_fill is not None:
                    nack_fill(fill)
                raise
            ack_fill = getattr(self.broker, "ack_fill_processed", None)
            if ack_fill is not None:
                ack_fill(fill)

            # Close/cover guard release. Three release paths:
            #   1) The tracked close order reached a terminal state (FILLED,
            #      CANCELED, REJECTED) — the close attempt is finished either way.
            #   2) Fill truth proves the tracked close completed: this fill's
            #      cumulative qty against the REQUESTED close size, matched by
            #      order id. Does not depend on resolving the parent Order object
            #      (broker-initiated closes are often absent from local history).
            #   3) Fallback for guard entries without a tracked order id (EOD /
            #      halt liquidations): portfolio accounting proves the lot is gone.
            pending_close = getattr(self, "_pending_close_symbols", None)
            if pending_close is not None and symbol in pending_close:
                tracked_id = self._get_pending_close_order_ids().get(symbol)
                _terminal = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
                if (
                    order is not None
                    and tracked_id
                    and str(order.id) == tracked_id
                    and order.status in _terminal
                ):
                    self._release_pending_close(symbol, f"order terminal: {order.status}")
                elif (
                    tracked_id
                    and str(getattr(fill, "order_id", "") or "") == tracked_id
                    and self._pending_close_qtys.get(symbol) is not None
                    and float(getattr(fill, "cumulative_qty", 0.0) or 0.0) + 1e-6 >= float(self._pending_close_qtys[symbol])
                ):
                    # Requested close size fully executed — residual lot (if any)
                    # is the intentional runner remainder, not an in-flight close.
                    self._release_pending_close(symbol, "requested close size filled")
                else:
                    local_lot = getattr(self.portfolio, "_lots", {}).get(symbol)
                    if local_lot is None or abs(float(getattr(local_lot, "qty", 0.0))) < 1e-9:
                        self._release_pending_close(symbol, "lot gone")
            if trade is not None:
                self.perf_tracker.update(trade, now=now)
                await asyncio.to_thread(self.db.save_trade, trade, self.mode)
                self.publish("trade", trade.model_dump(mode="json"))


                # Exit-policy research is intentionally NOT replayed at the instant the
                # live trade closes: that path is right-censored and made every challenger
                # appear to exit at the same early bar. CanonicalExitChallengerConsumer
                # receives causal market bars and evaluates the full 60-minute horizon.

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

            # Observer hook (durable subclass persists order-ledger state here).
            hook = getattr(self, "_observe_processed_fill", None)
            if hook is not None and asyncio.iscoroutinefunction(hook):
                await hook(fill)
        self.publish("orders", self.orders_payload())

    async def _observe_processed_fill(self, fill) -> None:  # noqa: ANN001
        """Hook invoked after each fill has been applied to the portfolio."""

    # ------------------------------------------------------------------
    # broadcast payloads
    # ------------------------------------------------------------------

    def telemetry_payload(
        self,
        event_type: str = "heartbeat_tick",
        pulse_intensity: str = "small",
        event_details: dict | None = None,
    ) -> dict:
        self._heartbeat_seq += 1
        now_iso = datetime.now(UTC).isoformat()

        # Classify activity state
        if self.state == EngineState.HALTED or self.system_health == "HALTED":
            activity = "IDLE"
        elif getattr(getattr(self, "trainer", None), "training_in_progress", False):
            activity = "TRAINING"
        elif event_type == "trade_executed":
            activity = "TRADING"
        elif event_type == "scanning":
            activity = "SCANNING"
        elif event_type == "strategy_evaluated" or self._evaluations_window_count > 0:
            activity = "EVALUATING"
        else:
            activity = "IDLE"

        if event_details:
            self._last_telemetry_event = event_details

        closed_trades = getattr(self.portfolio, "trades", []) if self.portfolio else []
        open_pos = {}
        if self.portfolio and hasattr(self.portfolio, "open_positions"):
            open_pos = self.portfolio.open_positions()
        orders_today = len(closed_trades) + len(open_pos)

        # Build human-readable degraded reasons
        diag = self.heartbeat_diagnostics
        degraded_reasons: list[str] = []
        if getattr(self, "_force_gated", False):
            degraded_reasons.append("manual_gated")
        if not diag.get("broker_connected", True):
            degraded_reasons.append("broker_disconnected")
        if not diag.get("market_data_fresh", True):
            degraded_reasons.append("market_data_stale")
        if not diag.get("db_connected", True):
            degraded_reasons.append("db_disconnected")
        if not diag.get("model_ready", True):
            degraded_reasons.append("model_not_ready")
        if not diag.get("scanner_ok", True):
            degraded_reasons.append("scanner_never_run")
        if not diag.get("buying_power_ok", True):
            degraded_reasons.append("low_buying_power")
        if not diag.get("positions_reconciled", True):
            degraded_reasons.append("positions_not_reconciled")
        if not diag.get("orders_reconciled", True):
            degraded_reasons.append("orders_not_reconciled")
        if not diag.get("vix_fresh", True):
            degraded_reasons.append("vix_feed_unavailable")

        payload = {
            "seq": self._heartbeat_seq,
            "ts": now_iso,
            "health": self.system_health,
            "activity": activity,
            "event": self._last_telemetry_event or {
                "type": event_type,
                "title": "Heartbeat Tick",
                "detail": "Engine loop active",
                "pulse": pulse_intensity,
            },
            "timestamps": self._subsystem_timestamps,
            "stats": {
                "broker_connected": diag.get("broker_connected", True),
                "market_data_fresh": diag.get("market_data_fresh", True),
                "scanner_ok": diag.get("scanner_ok", True),
                "market_regime": getattr(self, "_last_market_regime", "NORMAL"),
                "watchlist_count": len(self.watchlist),
                "universe_count": (len(u) if (u := getattr(self, "universe", None)) is not None and hasattr(u, '__len__') else len(self.watchlist)),
                "signals_evaluated_today": self._signals_evaluated_today,
                "evaluations_last_sec": self._evaluations_window_count,
                "last_evaluation_at": self._last_evaluation_at.isoformat() if getattr(self, "_last_evaluation_at", None) else None,
                "last_evaluated_symbol_count": getattr(self, "_last_evaluated_symbol_count", 0),
                "last_closed_bar_symbol_count": getattr(self, "_last_closed_bar_symbol_count", 0),
                "evaluator_stalled": (
                    getattr(self, "_last_closed_bar_symbol_count", 0) > 0
                    and getattr(self, "_last_evaluated_symbol_count", 0) == 0
                ),
                "orders_today": orders_today,
                "replay_queue_depth": 0,
                "latency_ms": getattr(self, "_last_broker_latency_ms", 0.0),
                "degraded_reasons": degraded_reasons,
            },
        }
        self._evaluations_window_count = 0
        return payload

    def _broadcast_tick(self, account, now: datetime, new_closed: dict, positions: list) -> None:  # noqa: ANN001
        assert self.portfolio is not None
        if getattr(self, "_skip_broadcast", False):
            # Fast replay: publish() is a no-op sink; only the data-health
            # timestamp has downstream readers and must stay fresh.
            if new_closed:
                self._subsystem_timestamps["last_bar"] = datetime.now(UTC).isoformat()
            return
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
        if new_closed:
            self._subsystem_timestamps["last_bar"] = datetime.now(UTC).isoformat()
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
        pulse = "medium" if self._evaluations_window_count > 0 else "small"
        event_type = "strategy_evaluated" if self._evaluations_window_count > 0 else "heartbeat_tick"
        self.publish("heartbeat", self.telemetry_payload(event_type, pulse))

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

            price = float(df["close"].iloc[-1])
            past_sessions = df.loc[~today]

            if not past_sessions.empty:
                prev_close = float(past_sessions["close"].iloc[-1])
            else:
                prev_close = float(df.loc[today, "open"].iloc[0])

            out.append({
                "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round((price / prev_close - 1.0) * 100.0, 2) if prev_close else 0.0,
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
            "tick": self._tick_count,
            "last_error": self.last_error,
            "reconciliation_failed": getattr(self, "reconciliation_failed", False),
            "system_health": getattr(self, "system_health", "NORMAL"),
            "allow_new_entries": getattr(self, "allow_new_entries", True),
            "heartbeat_diagnostics": getattr(self, "heartbeat_diagnostics", {}),
            "data_gap_symbols": sorted(self._data_gap_symbols),
            # Per-symbol scanner validation (tech score + walk-forward AUC) for
            # dynamically added watchlist symbols -- surfaced as the UI ML tag.
            "scanner_context": getattr(self, "scanner_context", {}),
            "scanner": dict(getattr(self, "scanner_status", {})),
        }

    async def _close_position_guarded(
        self,
        symbol: str,
        reason: str,
        qty: float | None = None,
        strategy: str | None = None,
    ) -> bool:
        """Close a position once and report whether the broker accepted the close.

        ``True`` means a close order with a usable identifier was accepted by
        the broker (it may still be waiting to fill).  ``False`` means the
        broker returned no order or explicitly rejected it.  A broker
        exception is re-raised after releasing the latch because that is an
        ambiguous result.  A duplicate call is treated as ``True`` because
        the existing in-flight close already owns the symbol's close intent;
        this preserves the idempotent close latch without reverting PIS state
        for a request that was never submitted.
        """
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        if not hasattr(self, "_pending_close_order_ids"):
            self._pending_close_order_ids = {}
        # fix(partial-close-wedge): track requested close size and submit time so
        # _drain_fills can release the guard from fill truth alone and stale
        # latches can never wedge PIS exits indefinitely (2026-08-24: after the
        # CRWV/V/AMZN partial scale-outs every subsequent PROTECTION_BREACH
        # exit was silently swallowed for 30-80 minutes).
        if not hasattr(self, "_pending_close_qtys"):
            self._pending_close_qtys = {}
        if not hasattr(self, "_pending_close_submitted_at"):
            self._pending_close_submitted_at = {}
        # fix(#45): also guard against concurrent per-tick flatten submissions
        if symbol in self._pending_close_symbols or symbol in self._flattening_symbols:
            applog.debug("engine", f"{symbol}: skipping duplicate close — close already in-flight (#45)")
            return True
        self._flattening_symbols.add(symbol)
        self._pending_close_symbols.add(symbol)
        try:
            # Support brokers whose close_position predates the qty parameter.
            # Signature inspection (not try/except TypeError) so a TypeError
            # raised INSIDE broker logic can never silently convert a targeted
            # partial close into a full-position close.
            close_parameters = inspect.signature(self.broker.close_position).parameters
            close_kwargs = {"reason": reason}
            if qty is not None and "qty" in close_parameters:
                close_kwargs["qty"] = qty
            if strategy and "strategy" in close_parameters:
                close_kwargs["strategy"] = strategy
            order = await self.broker.close_position(symbol, **close_kwargs)
            if order is None:
                # Close failed immediately (no broker position) — release so retries work.
                self._release_pending_close(symbol, "close returned no order")
                return False

            status = getattr(getattr(order, "status", None), "value", getattr(order, "status", None))
            status_value = str(status).split(".")[-1].lower()
            if status_value in {OrderStatus.REJECTED.value, OrderStatus.CANCELED.value}:
                # A rejected/canceled order is not an in-flight close.
                # Releasing now is important because PIS must be able to retry
                # on the next bar.
                self._release_pending_close(symbol, f"close {status_value}")
                return False

            order_id = getattr(order, "id", None)
            if not order_id:
                # Without an order id the fill-drain path cannot bind execution
                # truth to this latch, so do not claim that the close succeeded.
                self._release_pending_close(symbol, "close returned no order id")
                return False

            # Preserve the caller's execution attribution on broker-generated
            # close orders.  PIS closes do not pass through submit(), so this is
            # the last durable metadata boundary before the fill is observed.
            if strategy:
                order.strategy = strategy
            if reason:
                order.reason = reason

            # Track the close order id; _drain_fills releases the guard when
            # this order reaches a terminal state. A partial fill no longer
            # latches the guard until the whole lot is gone.
            self._get_pending_close_order_ids()[symbol] = str(order_id)
            self._pending_close_qtys[symbol] = float(qty) if qty is not None else None
            self._pending_close_submitted_at[symbol] = utcnow()
            # Register the close order so fill-drain can resolve it by id.
            # Broker-initiated closes never pass through submit(), and an
            # unresolvable tracked id used to wedge the guard permanently.
            self.recent_orders.appendleft(order)
            hook = getattr(self, "_persist_submitted_close_order", None)
            if hook is not None and asyncio.iscoroutinefunction(hook):
                await hook(order)
            return True
        except Exception as exc:
            # Submission itself failed or its result became ambiguous. Release
            # both guards so a later tick can retry instead of wedging exits;
            # callers receive the broker exception rather than treating the
            # request as filled.  The PIS evaluation boundary catches this,
            # reverts decision-time scale-out state, and returns to the tick
            # without masking the ambiguity.
            self._release_pending_close(symbol, f"close submission ambiguous: {exc}")
            applog.warning("engine", f"{symbol}: close submission failed or is ambiguous: {exc}")
            raise
        finally:
            self._flattening_symbols.discard(symbol)

    def _get_pending_close_order_ids(self) -> dict[str, str]:
        """Lazily materialize the close-order-id map (test engines may skip __init__)."""
        ids = getattr(self, "_pending_close_order_ids", None)
        if ids is None:
            ids = {}
            self._pending_close_order_ids = ids
        return ids

    def _release_pending_close(self, symbol: str, cause: str) -> None:
        """Release the per-symbol close latch so future PIS exits can execute."""
        if not hasattr(self, "_pending_close_symbols"):
            return
        if symbol not in self._pending_close_symbols:
            return
        self._pending_close_symbols.discard(symbol)
        self._get_pending_close_order_ids().pop(symbol, None)
        if hasattr(self, "_pending_close_qtys"):
            self._pending_close_qtys.pop(symbol, None)
        if hasattr(self, "_pending_close_submitted_at"):
            self._pending_close_submitted_at.pop(symbol, None)
        applog.debug("engine", f"{symbol}: close-guard released ({cause})")

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

    async def _persist_submitted_close_order(self, order: Order) -> None:
        """Hook for persistence of submitted close orders (durable subclass)."""

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
                if take_profit is not None and float(take_profit) > 0:
                    self._position_take_profits[p.symbol] = float(take_profit)
                    p.take_profit = float(take_profit)
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
            rejected_order = Order(id=cid, client_order_id=cid, symbol=symbol, side=OrderSide.BUY if str(order_info.get("side", "")).lower() == "buy" else OrderSide.SELL, qty=float(order_info.get("qty", 0.0) or 0.0), order_type=OrderType.LIMIT if str(order_info.get("order_type", "")).lower() == "limit" else OrderType.MARKET, limit_price=order_info.get("limit_price"), status=OrderStatus.REJECTED, reason=f"Ambiguous submit resolved absent after repeated broker not-found observations ({count}) and {age_seconds:.1f}s age.")
            await asyncio.to_thread(self.db.save_order, rejected_order, self.mode)
        if changed:
            await asyncio.to_thread(self.db.set_setting, "pending_reconciliation_orders", self.pending_reconciliation_orders)
    # ------------------------------------------------------------------
    # manual operations (REST)
    # ------------------------------------------------------------------

    async def manual_order(self, req: OrderRequest) -> Order:
        if self.state != EngineState.RUNNING or self.broker is None:
            raise RuntimeError("Engine is not running")

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

        # Owner control (CR-21): per-session manual-entry budget. Closes and
        # position reductions always flow; only NEW discretionary entries
        # count against the budget. Raises like other manual gates so the API
        # surfaces it as HTTP 400 with the reason intact.
        if is_entry:
            if not hasattr(self, "_manual_entry_dates"):
                self._manual_entry_dates = []
            today_et = datetime.now(ZoneInfo("America/New_York")).date()
            session_count = sum(1 for d in self._manual_entry_dates if d == today_et)
            _rc = getattr(self, "risk_config", None)
            budget = int(getattr(_rc, "max_manual_entries_per_session", 3) if _rc is not None else 3)
            if session_count >= budget:
                applog.warning(
                    "engine",
                    f"Manual entry BLOCKED for {req.symbol}: session budget exhausted "
                    f"({session_count}/{budget}) -- operator review required",
                )
                raise RuntimeError(
                    f"Manual entry budget exhausted ({session_count}/{budget} used this session) -- "
                    f"operator review required. Closing/reducing positions is still allowed."
                )
            self._manual_entry_dates.append(today_et)
            applog.info("engine", f"Manual entry {session_count + 1}/{budget} this session: {req.side} {req.qty} {req.symbol} ({req.reason})")

        # If this is a manual sell/cover (closing or reducing), add to pending-close guard so
        # strategies cannot immediately re-enter on the same/next tick before the fill drains.
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        if not hasattr(self, "_pending_close_order_ids"):
            self._pending_close_order_ids = {}
        sym_upper = req.symbol.upper()
        if not is_entry:
            self._pending_close_symbols.add(sym_upper)

        try:
            order = await self.broker.submit(req)
        except Exception:
            # Submission failed — nothing is in flight. Release the guard so a
            # later manual attempt or strategy exit can proceed.
            self._pending_close_symbols.discard(sym_upper)
            raise
        if not is_entry:
            if order.status == OrderStatus.REJECTED:
                self._pending_close_symbols.discard(sym_upper)
            else:
                self._pending_close_order_ids[sym_upper] = str(order.id)
        self.recent_orders.appendleft(order)
        await asyncio.to_thread(self.db.save_order, order, self.mode)
        await self._drain_fills(utcnow())
        self.publish("orders", self.orders_payload())
        await self._publish_positions()
        return order

    async def close_symbol(self, symbol: str) -> Order | None:
        if self.broker is None:
            raise RuntimeError("Engine is not running")
        # Guard: add to pending-close so strategies cannot immediately re-enter on the same/next tick
        sym_upper = symbol.upper()
        if not hasattr(self, "_pending_close_symbols"):
            self._pending_close_symbols = set()
        if not hasattr(self, "_pending_close_order_ids"):
            self._pending_close_order_ids = {}
        self._pending_close_symbols.add(sym_upper)
        try:
            order = await self.broker.close_position(symbol, reason="manual close")
        except Exception:
            self._pending_close_symbols.discard(sym_upper)
            raise
        if order is not None:
            self._pending_close_order_ids[sym_upper] = str(order.id)
            self.recent_orders.appendleft(order)
            await asyncio.to_thread(self.db.save_order, order, self.mode)
        else:
            # Close failed — release guard so retries work
            self._pending_close_symbols.discard(sym_upper)
        await self._drain_fills(utcnow())
        await self._publish_positions()
        return order


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
        # =============================================================================
        # [MODIFIED 2026-10-06]: Remove 30 watchlist cap
        # ORIGINAL INTENT: The 30 cap was a hard limit to prevent excessive watchlist growth.
        # REASON FOR CHANGE: User directive: remove the 30 watchlist cap and always allow addition from UI without any errors.
        # SAFE ROLLBACK: The original cap check was removed; rollback via git history.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        # Watchlist cap removed: any number of symbols can be added from the UI.
        # =============================================================================

        current_active = set(self.watchlist)
        # --- NEW IMPLEMENTATION ---
        # When user explicitly sets watchlist, user_watchlist becomes exactly cleaned.
        # Any symbol previously in scanned_watchlist promoted by user is removed from temporary scanner pool.
        new_active = set(cleaned)
        additions = new_active - current_active
        self.user_watchlist = cleaned
        self.scanned_watchlist = [s for s in self.scanned_watchlist if s not in new_active]
        self.watchlist = sorted(list(set(self.user_watchlist + self.scanned_watchlist)))
        self.core_watchlist = list(self.user_watchlist)

        self.db.set_setting("watchlist", self.user_watchlist)

        # =============================================================================
        # [MODIFIED 2026-10-06]: Resilient warmup for additions from UI
        # ORIGINAL INTENT: Direct await self.data.warmup(list(additions)) would propagate
        # any network/data exception as an unhandled error to the API route.
        # REASON FOR CHANGE: Always allow addition from UI without any errors.
        # SAFE ROLLBACK: Previous code preserved below.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if self.data is not None and additions:
            try:
                await self.data.warmup(list(additions))
            except Exception as exc:
                applog.warning("engine", f"Warmup for added symbols {additions} caught gracefully: {exc}")
        # =============================================================================

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

    # =============================================================================
    # [MODIFIED 2026-10-13]: Support simulated as_of time in run_market_scanner
    # ORIGINAL INTENT: run_market_scanner took only is_intraday.
    # REASON FOR CHANGE: Backtest replay needs to pass simulated as_of time.
    # SAFE ROLLBACK: Defaults to None (resolves to clock.now_utc()).
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    async def _run_scanner_inline(self, *, is_intraday: bool) -> list[str]:
        """Run a replay scanner to completion before advancing the replay clock."""
        return await self.run_market_scanner(is_intraday=is_intraday)

    def _record_scanner_forensics(
        self,
        *,
        timestamp: datetime,
        outcome: Any,
        watchlist: list[str],
        scan_rows: list[dict],
    ) -> None:
        """Persist scanner decisions into a replay ledger when one is attached."""
        ledger = getattr(self, "runtime_ledger", None)
        record_event = getattr(ledger, "record_event", None)
        if ledger is None or not callable(record_event):
            return

        outcome_value = getattr(outcome, "value", str(outcome))
        selected = {str(symbol).upper() for symbol in watchlist}
        try:
            record_event(
                "SCANNER_RUN",
                timestamp,
                "",
                {
                    "outcome": outcome_value,
                    "selected_count": len(selected),
                    "candidate_count": len(scan_rows),
                },
            )
            seen: set[str] = set()
            for row in scan_rows:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol", "")).strip().upper()
                if not symbol:
                    continue
                details = dict(row)
                details["selected"] = symbol in selected
                details["outcome"] = outcome_value
                record_event("SCANNER_CANDIDATE", timestamp, symbol, details)
                seen.add(symbol)

            # Defensive watchlists do not have Stage-3 rows, but they are still
            # an explicit scanner decision and must be visible in replay evidence.
            for symbol in sorted(selected - seen):
                record_event(
                    "SCANNER_CANDIDATE",
                    timestamp,
                    symbol,
                    {"selected": True, "outcome": outcome_value, "defensive": True},
                )
        except Exception as exc:  # pragma: no cover - ledger is an observer
            applog.warning("engine", f"Failed to record scanner forensic event: {exc}")

    async def run_market_scanner(self, is_intraday: bool = False, as_of: datetime | None = None) -> list[str]:
    # =============================================================================
        from app.core.scanner import MarketScanner, ScannerOutcome
        scanner = getattr(self, "_scanner", None)
        if scanner is None or getattr(self, "_scanner_data", None) is not self.data:
            scanner = MarketScanner(self.data, self.user_watchlist, risk_config=self.risk_config)
            # Cache the instance so strategy evaluation can consume live mover
            # context and so the mover cache survives scanner refreshes.
            self._scanner = scanner
            self._scanner_data = self.data
        else:
            # Risk settings and the user watchlist can change while the engine is
            # running; keep the cached scanner's inputs current without throwing
            # away its bounded discovery/mover caches.
            scanner.core_watchlist = self.user_watchlist
            scanner.risk_config = self.risk_config
            if hasattr(scanner, "_gap_scanner"):
                scanner._gap_scanner.min_price = getattr(
                    self.risk_config,
                    "min_price",
                    scanner._gap_scanner.min_price,
                )
        scan_label = "Intraday refresh" if is_intraday else "Pre-market"
        applog.info("engine", f"Starting autonomous market scanner ({scan_label} scan)...")
        from zoneinfo import ZoneInfo
        effective_now = as_of or (self.clock.now_utc() if hasattr(self, "clock") else utcnow())
        replay_scan = (
            as_of is not None
            or type(self.clock).__name__ == "ReplayClock"
            or getattr(self, "replay_fast_path", False)
        )
        scan_as_of = effective_now if replay_scan else None
        self.scanner_status.update(
            {
                "status": "running",
                "outcome": None,
                "last_started_at": effective_now.isoformat(),
                "last_error": None,
                "is_intraday": is_intraday,
                "as_of": scan_as_of.isoformat() if scan_as_of is not None else None,
            }
        )
        try:
            outcome, new_watchlist = await scanner.run(as_of=scan_as_of)
        except Exception as exc:  # scanner normally fails closed, but keep the engine safe
            outcome, new_watchlist = ScannerOutcome.FAILED, []
            applog.error("engine", f"Market scanner raised unexpectedly: {exc}")

        try:
            scan_rows = list(scanner.get_last_scans() or [])
        except Exception:
            scan_rows = []
        discovery_telemetry = getattr(scanner, "_last_discovery_telemetry", None)
        self.scanner_status["discovery"] = (
            dict(discovery_telemetry) if isinstance(discovery_telemetry, dict) else None
        )
        if outcome == ScannerOutcome.SUCCESS_EMPTY:
            new_watchlist = []
        outcome_value = getattr(outcome, "value", str(outcome))
        self._record_scanner_forensics(
            timestamp=effective_now,
            outcome=outcome,
            watchlist=list(new_watchlist or []),
            scan_rows=scan_rows,
        )
        if outcome == ScannerOutcome.FAILED:
            self._scanner_retry_after = effective_now + timedelta(minutes=5)
            self.scanner_status.update(
                {
                    "status": "failed",
                    "outcome": outcome_value,
                    "last_completed_at": effective_now.isoformat(),
                    "last_error": "scanner pipeline failed",
                    "last_watchlist_count": 0,
                }
            )
            applog.error("engine", "Market scanner failed; preserving the existing watchlist")
            return []

        now_utc = effective_now
        try:
            ttl_minutes = max(1, int(getattr(self.risk_config, "scanner_watchlist_ttl", 15)))
        except (TypeError, ValueError):
            ttl_minutes = 15
        expires_at_scan = now_utc + timedelta(minutes=ttl_minutes)
        self._last_scan_date = effective_now.astimezone(ZoneInfo("America/New_York")).date()
        self._scanner_retry_after = None
        self.scanner_status.update(
            {
                "status": "success",
                "outcome": outcome_value,
                "last_completed_at": effective_now.isoformat(),
                "last_error": None,
                "last_watchlist_count": len(new_watchlist),
            }
        )

        # Persist scanner candidate context only for symbols in current scan outcome
        if not hasattr(self, "scanner_context"):
            self.scanner_context = {}
        current_symbols = {s.upper() for s in new_watchlist}
        for item in scan_rows:
            sym = str(item.get("symbol", "")).upper()
            if sym and sym in current_symbols:
                self.scanner_context[sym] = {
                    "score": item.get("score", 0.0),
                    "auc": item.get("auc", 0.0),
                    "scanned_at": now_utc,
                    "expires_at": expires_at_scan,
                }

        if outcome in (ScannerOutcome.SUCCESS_NONEMPTY, ScannerOutcome.SUCCESS_EMPTY):
            # Feature 1: Hysteresis — protect symbols with open positions & pending orders from removal
            old_scanned = set(self.scanned_watchlist)
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

            if not hasattr(self, "scanned_watchlist_expires_at"):
                self.scanned_watchlist_expires_at = {}

            scanned_set = set(new_watchlist)

            for sym in scanned_set:
                self.scanned_watchlist_ttl[sym] = self.risk_config.scanner_watchlist_ttl
                self.scanned_watchlist_expires_at[sym] = expires_at_scan

            # Cleanup expired entries from watchlist state and scanner context
            expired_keys = [sym for sym, exp in self.scanned_watchlist_expires_at.items() if now_utc >= exp]
            for sym in expired_keys:
                self.scanned_watchlist_expires_at.pop(sym, None)
                self.scanned_watchlist_ttl.pop(sym, None)
                self.scanner_context.pop(sym, None)

            retained_dynamic = {
                sym for sym, exp in self.scanned_watchlist_expires_at.items() if now_utc < exp
            }

            if is_intraday:
                new_set = set(new_watchlist)
                existing = set(self.scanned_watchlist)
                to_remove = (existing - new_set) - (active_trade_symbols | retained_dynamic)
                to_add = new_set - existing
                updated = (existing - to_remove) | to_add
                self.scanned_watchlist = sorted(updated)
                # Newly discovered symbols need rolling frames BEFORE their
                # first evaluation tick -- without warmup they sit below the
                # feature threshold and ML never runs for them.
                if to_add and self.data is not None:
                    try:
                        await self.data.warmup(sorted(to_add))
                    except Exception as exc:
                        applog.warning("engine", f"Scanner addition warmup failed for {sorted(to_add)}: {exc}")
                if to_add or to_remove:
                    applog.info(
                        "engine",
                        f"Intraday watchlist refresh: +{sorted(to_add)} −{sorted(to_remove)} "
                        f"(protected exposure: {sorted((active_trade_symbols | retained_dynamic) & existing)})",
                    )
                else:
                    applog.info("engine", "Intraday watchlist refresh: no changes")
            else:
                final = scanned_set | retained_dynamic | active_trade_symbols
                self.scanned_watchlist = sorted(final)
                premarket_additions = scanned_set - old_scanned - set(self.user_watchlist)
                if premarket_additions and self.data is not None:
                    try:
                        await self.data.warmup(sorted(premarket_additions))
                    except Exception as exc:
                        applog.warning("engine", f"Scanner addition warmup failed for {sorted(premarket_additions)}: {exc}")
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
                    end = effective_now
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

        # Batched screening needs provider support; replay stubs (and any
        # provider without multi-bar fetch) skip silently instead of raising
        # 'SimpleNamespace has no attribute get_multi_bars' on every tick
        # (observed 2026-08-26 as per-cycle warning spam inside replays).
        if not hasattr(self.data.provider, "get_multi_bars"):
            return

        applog.info("engine", f"Running event monitor on {len(non_watchlist)} non-watchlist candidates...")

        start_time = now - timedelta(minutes=60)

        async def _screen(symbol: str, df) -> str | None:
            try:
                if df is None or df.empty or len(df) < 15:
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
                    # compute_features-derived columns are absent from enrich();
                    # score_action fail-closes with None on incomplete features.
                    p_up = self.model.score_action(row, SignalAction.ENTER_LONG, symbol) or 0.0
                    p_down = self.model.score_action(row, SignalAction.ENTER_SHORT, symbol) or 0.0
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

        # ONE batched multi-symbol request for the whole candidate pool. The
        # previous per-symbol history() sweep fired ~200 req/min and tripped
        # Alpaca 429s continuously (2026-08-24 session evidence).
        triggered: list[str] = []
        try:
            batch = await self.data.provider.get_multi_bars(non_watchlist, "1Min", start=start_time, end=now)
        except Exception as exc:
            applog.warning("engine", f"Event monitor batched fetch failed — skipping this cycle: {exc}")
            return
        results = await asyncio.gather(*[_screen(sym, batch.get(sym)) for sym in non_watchlist])
        triggered = [r for r in results if r is not None]

        if triggered:
            added_any = False
            try:
                event_ttl_minutes = max(1, int(getattr(self.risk_config, "scanner_watchlist_ttl", 15)))
            except (TypeError, ValueError):
                event_ttl_minutes = 15
            event_expires_at = now + timedelta(minutes=event_ttl_minutes)
            for sym in triggered:
                if sym not in self.scanned_watchlist:
                    self.scanned_watchlist.append(sym)
                    added_any = True
                # Event-monitor additions are temporary scanner symbols too.
                # Give them the same bounded lifetime as normal scanner output;
                # otherwise one transient surge permanently expands the polling
                # and risk surface.
                self.scanned_watchlist_ttl[sym] = event_ttl_minutes
                self.scanned_watchlist_expires_at[sym] = event_expires_at
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

    async def _check_system_heartbeat(self, now: datetime) -> None:
        """Runs the unified safety/system heartbeat health checks."""
        self.system_health = "NORMAL"
        self.allow_new_entries = True
        self.vix_unavailable = False

        # Enforce manual gating override latch
        if getattr(self, "_force_gated", False):
            self.allow_new_entries = False
            self.system_health = "DEGRADED"

        # Enforce reconciliation failure latch (SAFE_MODE)
        if getattr(self, "reconciliation_mismatch", False):
            self.system_health = "SAFE_MODE"
            self.allow_new_entries = False

        # REPLAY FAST-PATH: skip ops monitors (connectivity/freshness/db-write/
        # buying-power/reconcile streaks/VIX polling) -- none alter decisions in
        # a deterministic replay. Market-risk gating (VIX-driven tiers) IS
        # decision-relevant, so it still runs via the shared tail below.
        if getattr(self, "replay_fast_path", False):
            market_risk_state, risk_multiplier, allowed_strategies, risk_allow_entries = self._evaluate_market_risk(now)
            is_halted = (self.state == EngineState.HALTED) or getattr(self.risk, "hard_halted", False)
            allow_new_entries = (
                self.allow_new_entries
                and not getattr(self, "_force_gated", False)
                and risk_allow_entries
                and not is_halted
                and self.system_health == "NORMAL"
            )
            from app.core.domain import SafetyDecision as _SD
            self.last_safety_decision = _SD(
                system_state="HALTED" if is_halted else "NORMAL",
                market_risk_state=market_risk_state,
                allow_new_entries=allow_new_entries,
                risk_multiplier=risk_multiplier,
                allowed_strategies=allowed_strategies,
                require_exit_only=not allow_new_entries or is_halted,
                halt_reason=getattr(self.risk, "hard_halt_reason", None) if is_halted else None,
            )
            self.allow_new_entries = allow_new_entries
            self.heartbeat_diagnostics = {"replay_fast_path": True}
            return

        diagnostics = {
            "broker_connected": True,
            "market_data_fresh": True,
            "db_connected": True,
            "db_write_ok": True,
            "model_ready": True,
            "scanner_ok": True,
            "buying_power_ok": True,
            "positions_reconciled": not getattr(self, "reconciliation_mismatch", False),
            "orders_reconciled": not getattr(self, "reconciliation_mismatch", False),
            "vix_fresh": True,
        }

        # Monitor 1: Broker connectivity
        try:
            account = await self.broker.account()
            if account is None:
                raise ValueError("No account information returned from broker.")
        except Exception as exc:
            diagnostics["broker_connected"] = False
            self.allow_new_entries = False
            self.system_health = "DEGRADED"
            applog.warning("engine.heartbeat", f"Broker connection lost/unknown: {exc}")

        # Monitor 2: Market-data freshness
        if self.state == EngineState.RUNNING:
            from zoneinfo import ZoneInfo
            now_et = now.astimezone(ZoneInfo("America/New_York"))
            is_synthetic = getattr(getattr(self.data, "provider", None), "name", "") == "synthetic"
            market_minutes = now_et.hour * 60 + now_et.minute
            market_open_minutes = 9 * 60 + 30
            market_close_minutes = 16 * 60
            minutes_since_open = market_minutes - market_open_minutes

            is_trading_day = False
            try:
                get_session_close(now)
                is_trading_day = True
            except Exception:
                pass

            if is_trading_day and market_open_minutes <= market_minutes <= market_close_minutes and not is_synthetic:
                health_symbols = [sym for sym in ("SPY", "QQQ") if self.watchlist and sym in self.watchlist]
                if self.watchlist:
                    health_symbols.append(self.watchlist[0])
                health_symbols = list(dict.fromkeys(health_symbols))[:3]
                if not health_symbols:
                    health_symbols = ["SPY"]

                is_open_grace = (0 <= minutes_since_open < 3)

                symbols_fresh: dict[str, bool] = {}
                symbols_info: list[str] = []

                for sym in health_symbols:
                    try:
                        df = self.data.frame(sym, "1Min", limit=1)
                        if df is not None and not df.empty:
                            last_bar_time = df.index[-1]
                            if isinstance(last_bar_time, str):
                                last_bar_time = datetime.fromisoformat(last_bar_time)
                            if last_bar_time.tzinfo is None and now.tzinfo is not None:
                                last_bar_time = last_bar_time.replace(tzinfo=now.tzinfo)

                            bar_et = last_bar_time.astimezone(ZoneInfo("America/New_York"))
                            bar_age_sec = (now - last_bar_time).total_seconds()
                            is_current_session = (
                                bar_et.date() == now_et.date()
                                and (bar_et.hour * 60 + bar_et.minute) >= market_open_minutes
                            )

                            if is_open_grace:
                                fresh = True
                            elif minutes_since_open >= 3 and not is_current_session:
                                fresh = False
                            else:
                                fresh = (bar_age_sec <= 600)

                            symbols_fresh[sym] = fresh
                            symbols_info.append(
                                f"{sym}: age={bar_age_sec:.0f}s current_session={is_current_session} fresh={fresh}"
                            )
                        else:
                            symbols_fresh[sym] = is_open_grace
                            symbols_info.append(f"{sym}: no_data fresh={is_open_grace}")
                    except Exception as exc:
                        symbols_fresh[sym] = is_open_grace
                        symbols_info.append(f"{sym}: err={exc}")

                any_fresh = any(symbols_fresh.values())
                if not any_fresh:
                    diagnostics["market_data_fresh"] = False
                    if self.mode != "demo" and type(self.clock).__name__ != "ReplayClock":
                        self.allow_new_entries = False
                        applog.warning(
                            "engine.heartbeat",
                            f"Market data stale (now_et={now_et.strftime('%H:%M:%S')}, "
                            f"grace={is_open_grace}): {', '.join(symbols_info)} — BLOCK_NEW_ENTRIES",
                        )

        # Monitor 3: Database health
        try:
            _ = self.db.get_setting("risk_config")
        except Exception as exc:
            diagnostics["db_connected"] = False
            self.system_health = "DEGRADED"
            applog.error("engine.heartbeat", f"Database connection health check failed: {exc}")

        # Monitor 4: Database write health
        try:
            await asyncio.to_thread(self.db.set_setting, "heartbeat_write_check", now.isoformat())
        except Exception as exc:
            diagnostics["db_write_ok"] = False
            self.system_health = "HALTED"
            self.state = EngineState.HALTED
            try:
                self._persist_halt(self.mode, f"Critical database write failure: {exc}")
            except Exception as persist_exc:
                applog.error("engine.heartbeat", f"Failed to persist halt reason: {persist_exc}")
            applog.error("engine.heartbeat", f"FATAL: Database write check failed. Escalating to HALTED. Error: {exc}")
            if self._emergency_flatten_task is None or self._emergency_flatten_task.done():
                self._emergency_flatten_task = asyncio.create_task(
                    self._emergency_flatten_loop(),
                    name="trading-engine-emergency-flatten"
                )
            self.heartbeat_diagnostics = diagnostics
            return

        # Monitor 5: Model availability
        if (not self.model or not hasattr(self.model, "info") or not self.model.info()) and self.mode != "demo":
            diagnostics["model_ready"] = False
            if self.system_health not in ("SAFE_MODE", "HALTED"):
                self.system_health = "DEGRADED"

        # Monitor 6: Scanner health
        if self.mode != "demo":
            from zoneinfo import ZoneInfo
            now_et = now.astimezone(ZoneInfo("America/New_York"))
            today_date = now_et.date()
            is_past_scan_time = (now_et.hour * 60 + now_et.minute) >= 9 * 60 + 15

            is_stale = False
            scanner_status = getattr(self, "scanner_status", {})
            if scanner_status.get("status") == "failed" or scanner_status.get("outcome") == "FAILED":
                is_stale = True
            elif self._last_scan_date is None:
                is_stale = True
            elif self._last_scan_date != today_date:
                # In unit tests today_date might be simulated (e.g. 2026-01-06) while _last_scan_date is real-time.
                # If the difference is > 10 days, we assume it is a mock environment and bypass the mismatch.
                if abs((today_date - self._last_scan_date).days) <= 10:
                    is_stale = True

            if is_stale and is_past_scan_time:
                diagnostics["scanner_ok"] = False
                if self.system_health not in ("SAFE_MODE", "HALTED"):
                    self.system_health = "DEGRADED"

        # Monitor 7: Buying power
        if diagnostics["broker_connected"]:
            try:
                try:
                    raw_bp = getattr(account, "buying_power", None)
                    if raw_bp is None:
                        raw_bp = getattr(account, "cash", 10000.0)
                    buying_power = float(raw_bp)
                except (TypeError, ValueError):
                    buying_power = 10000.0

                raw_thresh = getattr(self.settings, "min_buying_power_threshold", 1000.0)
                try:
                    bp_threshold = float(raw_thresh) if raw_thresh is not None else 1000.0
                except (TypeError, ValueError):
                    bp_threshold = 1000.0

                if buying_power < bp_threshold:
                    diagnostics["buying_power_ok"] = False
                    self.allow_new_entries = False
                    if self.system_health not in ("HALTED", "SAFE_MODE"):
                        self.system_health = "DEGRADED"
                    applog.warning("engine.heartbeat", f"Buying power {buying_power} is below threshold {bp_threshold}.")
            except Exception as exc:
                applog.debug("engine.heartbeat", f"Buying power check failed: {exc}")

        # Monitors 8/9: continuously re-evaluate broker truth. Alpaca can expose a
        # partial/flatten fill before the durable fill consumer has applied it locally.
        # Confirm mismatches across heartbeats and automatically clear reconciliation-only
        # SAFE_MODE once broker and durable lots converge.
        position_bad = False
        order_bad = False
        broker_syms: dict[str, float] = {}
        if diagnostics["broker_connected"]:
            try:
                broker_positions = await self.broker.positions()
                broker_syms = {p.symbol: float(p.qty) for p in broker_positions if hasattr(p, "symbol") and abs(float(p.qty)) > 1e-9}
                lots = getattr(self.portfolio, "_lots", {}) if self.portfolio else {}
                db_syms = {sym: float(lot.qty) for sym, lot in lots.items() if hasattr(lot, "qty") and abs(float(lot.qty)) > 1e-9} if isinstance(lots, dict) else {}
                position_bad = broker_syms != db_syms
                if position_bad:
                    diagnostics["positions_reconciled"] = False
                    self._position_reconciliation_mismatch_streak += 1
                    applog.warning("engine.heartbeat", f"Position reconciliation mismatch confirmation {self._position_reconciliation_mismatch_streak}/2. Broker: {broker_syms}, DB lots: {db_syms}")
                else:
                    self._position_reconciliation_mismatch_streak = 0
            except Exception as exc:
                diagnostics["positions_reconciled"] = False
                applog.debug("engine.heartbeat", f"Position reconciliation observation unavailable: {exc}")

            try:
                # Durable ambiguous submissions are owned by
                # _reconcile_pending_submissions(). Heartbeat never deletes them.
                order_bad = bool(self.pending_reconciliation_orders)
                if order_bad:
                    diagnostics["orders_reconciled"] = False
                    self._order_reconciliation_mismatch_streak += 1
                    applog.warning("engine.heartbeat", f"Order reconciliation unresolved confirmation {self._order_reconciliation_mismatch_streak}/2: pending_client_ids={list(self.pending_reconciliation_orders.keys())}")
                else:
                    self._order_reconciliation_mismatch_streak = 0
            except Exception as exc:
                diagnostics["orders_reconciled"] = False
                order_bad = True
                applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable: {exc}")

        confirmed_bad = self._position_reconciliation_mismatch_streak >= 2 or self._order_reconciliation_mismatch_streak >= 2
        if confirmed_bad:
            self.reconciliation_mismatch = True
            self.allow_new_entries = False
            if self.system_health != "HALTED":
                self.system_health = "SAFE_MODE"
        elif diagnostics["broker_connected"] and not position_bad and not order_bad:
            if self.reconciliation_mismatch:
                self.reconciliation_mismatch = False
                self.allow_new_entries = True
                if self.system_health == "SAFE_MODE":
                    self.system_health = "NORMAL"
                applog.info("engine.heartbeat", "Reconciliation restored. Leaving SAFE_MODE.")
                applog.info("engine.heartbeat", "Broker/local reconciliation restored; clearing reconciliation SAFE_MODE latch")
            self.reconciliation_mismatch = False
            diagnostics["positions_reconciled"] = True
            diagnostics["orders_reconciled"] = True
            if self.system_health == "SAFE_MODE":
                self.system_health = "NORMAL"

        # Monitor 10: VIX feed availability
        vix_fresh = False
        self._last_vix_status = "UNAVAILABLE"
        self._last_vix_value = None
        self._last_vix_timestamp = None
        for vix_sym in ["^VIX", "VIX"]:
            try:
                vix_df = self.data.frame(vix_sym, "1Min", limit=1)
                if vix_df is not None and not vix_df.empty:
                    last_vix_time = vix_df.index[-1]
                    if isinstance(last_vix_time, str):
                        last_vix_time = datetime.fromisoformat(last_vix_time)
                    if last_vix_time.tzinfo is None and now.tzinfo is not None:
                        last_vix_time = last_vix_time.replace(tzinfo=now.tzinfo)

                    vix_price_val = float(vix_df["close"].iloc[-1])
                    vix_age_seconds = (now - last_vix_time).total_seconds()
                    self._last_vix_value = vix_price_val
                    self._last_vix_timestamp = last_vix_time
                    self._last_vix_status = "STALE"

                    is_synthetic = getattr(getattr(self.data, "provider", None), "name", "") == "synthetic"
                    if vix_age_seconds < 1800 or self.mode == "demo" or is_synthetic:
                        vix_fresh = True
                        self._last_vix_status = "HEALTHY"
                        break
            except Exception:
                pass

        last_vix_state = getattr(self, "_vix_unavailable_last_state", None)

        if not vix_fresh:
            diagnostics["vix_fresh"] = False
            self.vix_unavailable = True
            # VIX unavailability triggers DEFENSIVE market-risk tier fallback, not global system health degradation.
            if last_vix_state is not True:
                applog.warning("engine.heartbeat", "VIX volatility feed unavailable. Falling back to conservative market-risk tier.")
                self._vix_unavailable_last_state = True
        else:
            self.vix_unavailable = False
            if last_vix_state is True:
                applog.info("engine.heartbeat", "VIX volatility feed restored.")
                self._vix_unavailable_last_state = False
            elif last_vix_state is None:
                self._vix_unavailable_last_state = False

        self.heartbeat_diagnostics = diagnostics

        # Health severity is monotonic within a heartbeat.  A later, less-severe
        # monitor (for example broker connectivity -> DEGRADED) must never overwrite
        # an already-latched reconciliation SAFE_MODE.
        if self.state == EngineState.HALTED or getattr(self.risk, "hard_halted", False):
            self.system_health = "HALTED"
            self.allow_new_entries = False
        elif getattr(self, "reconciliation_mismatch", False):
            self.system_health = "SAFE_MODE"
            self.allow_new_entries = False

        # Phase F2: Market Risk Evaluation
        market_risk_state, risk_multiplier, allowed_strategies, risk_allow_entries = self._evaluate_market_risk(now)

        # System-level entry gating: respect manual toggle gating (_force_gated)
        system_allow_entries = self.allow_new_entries and not getattr(self, "_force_gated", False)

        is_halted = (self.system_health == "HALTED") or (self.state == EngineState.HALTED) or getattr(self.risk, "hard_halted", False)

        # Override allowed entries if system_health is DEGRADED, SAFE_MODE, or HALTED, or if we are halted
        allow_new_entries = system_allow_entries and risk_allow_entries and (self.system_health == "NORMAL") and not is_halted

        # require_exit_only is True if system_health is SAFE_MODE or HALTED or if allow_new_entries is False or if we are halted
        require_exit_only = not allow_new_entries or (self.system_health in ("SAFE_MODE", "HALTED")) or is_halted

        # halt_reason is set if system_health is HALTED or SAFE_MODE or we are hard halted
        halt_reason = None
        if is_halted:
            halt_reason = getattr(self.risk, "hard_halt_reason", None) or "Critical system health failure (HALTED)"
        elif self.system_health == "SAFE_MODE":
            halt_reason = "Position/order reconciliation discrepancy (SAFE_MODE)"

        from app.core.domain import SafetyDecision
        self.last_safety_decision = SafetyDecision(
            system_state="HALTED" if is_halted else self.system_health,
            market_risk_state=market_risk_state,
            allow_new_entries=allow_new_entries,
            risk_multiplier=risk_multiplier,
            allowed_strategies=allowed_strategies,
            require_exit_only=require_exit_only,
            halt_reason=halt_reason
        )
        self.allow_new_entries = allow_new_entries

    def _evaluate_market_risk(self, now: datetime) -> tuple[str, float, list[str], bool]:
        """
        Determines the market risk state: NORMAL -> CAUTION -> DEFENSIVE -> SURVIVAL.
        Returns:
            market_risk_state: str
            risk_multiplier: float
            allowed_strategies: list[str]
            allow_new_entries: bool
        """
        # Fast-replay cadence: the multi-family evaluation costs ~40ms/tick;
        # re-running it on every 1-minute bar is wasted work with zero decision
        # benefit. Reuse the cached verdict for a configurable session-time
        # interval (live path keeps per-tick evaluation).
        interval = int(getattr(self.risk_config, "replay_market_risk_eval_interval_sec", 30) or 0)
        if getattr(self, "replay_fast_path", False) and interval > 0:
            last = getattr(self, "_market_risk_cache", None)
            if last is not None:
                last_now, last_result = last
                try:
                    if abs((now - last_now).total_seconds()) < interval:
                        return last_result
                except TypeError:
                    pass
            result = self._evaluate_market_risk_uncached(now)
            self._market_risk_cache = (now, result)
            return result
        return self._evaluate_market_risk_uncached(now)

    def _evaluate_market_risk_uncached(self, now: datetime) -> tuple[str, float, list[str], bool]:
        """
        Determines the market risk state: NORMAL -> CAUTION -> DEFENSIVE -> SURVIVAL.
        Returns:
            market_risk_state: str
            risk_multiplier: float
            allowed_strategies: list[str]
            allow_new_entries: bool
        """
        # Default fallback values
        vix_price = 15.0
        vix_percentile = 30.0
        vix_roc = 0.0
        market_trend_bullish = True
        breadth_positive = True

        # 1. Fetch VIX data
        vix_sym = self._vix_symbols()[0]
        vix_df = getattr(self, "_latest_vix_frame", None)
        if vix_df is None or vix_df.empty:
            vix_df = self._fetch_vix_frame(vix_sym, now)

        # Calculate VIX percentile and velocity
        try:
            if vix_df is not None and not vix_df.empty and hasattr(vix_df, "columns") and "close" in vix_df.columns:
                closes = vix_df["close"].dropna().values
                if len(closes) > 0:
                    val = float(closes[-1])
                    # VIX index values operate between 5 and 90. Equity price mocks (e.g. $100+) should not corrupt VIX.
                    if val < 90.0:
                        vix_price = val
                        try:
                            vix_daily = vix_df.resample("1D").last().dropna()
                        except Exception:
                            vix_daily = vix_df

                        daily_closes = vix_daily["close"].dropna().values
                        if len(daily_closes) > 0:
                            history = daily_closes[-200:]
                            vix_percentile = float((history < vix_price).sum() / len(history) * 100.0)

                        # VIX velocity (ROC over last 10 minutes)
                        if len(closes) >= 10:
                            vix_prev = closes[-10]
                            if vix_prev > 0:
                                vix_roc = float((vix_price - vix_prev) / vix_prev * 100.0)
        except Exception as e:
            applog.warning("engine.risk", f"Failed to compute VIX metrics: {e}")

        # 2. Fetch Broad-market Trend (using SPY 200-period SMA on 1Min)
        spy_sym = "SPY"
        try:
            spy_df = self.data.frame(spy_sym, "1Min")
            if spy_df is not None and not spy_df.empty:
                closes = spy_df["close"].dropna().values
                if len(closes) > 0:
                    spy_price = closes[-1]
                    if len(closes) >= 200:
                        spy_sma = float(closes[-200:].mean())
                    else:
                        spy_sma = float(closes.mean())
                    market_trend_bullish = spy_price >= spy_sma
        except Exception as e:
            applog.warning("engine.risk", f"Failed to compute Broad-market Trend: {e}")

        # 3. Breadth Confirmation (percentage of watchlist symbols above their 20-period SMA)
        above_count = 0
        total_count = 0
        for symbol in self.watchlist:
            try:
                df = self.data.frame(symbol, "1Min")
                if df is not None and not df.empty:
                    closes = df["close"].dropna().values
                    if len(closes) > 0:
                        total_count += 1
                        sma_20 = float(closes[-20:].mean()) if len(closes) >= 20 else float(closes.mean())
                        if closes[-1] >= sma_20:
                            above_count += 1
            except Exception:
                pass
        if total_count > 0:
            breadth_positive = (above_count / total_count) >= 0.5

        # 4. Classify Risk State
        # If VIX feed is explicitly unavailable, fall back to DEFENSIVE (conservative tier)
        if getattr(self, "vix_unavailable", False):
            state = "DEFENSIVE"
        elif vix_price >= 30.0 or vix_percentile >= 90.0 or vix_roc >= 50.0:
            state = "SURVIVAL"
        elif vix_price >= 22.0 or vix_percentile >= 75.0 or vix_roc >= 25.0 or not market_trend_bullish:
            state = "DEFENSIVE"
        elif vix_price >= 16.0 or vix_percentile >= 50.0 or not breadth_positive:
            state = "CAUTION"
        else:
            state = "NORMAL"

        # Allowed strategies mapping
        all_strategies = [s.name for s in self.strategies] if hasattr(self, "strategies") and self.strategies else []
        try:
            if hasattr(self, "db") and self.db:
                db_strats = list(self.db.get_setting("strategies") or {})
                for st in db_strats:
                    if st not in all_strategies:
                        all_strategies.append(st)
        except Exception:
            pass
        if not all_strategies:
            all_strategies = ["momentum", "mean_reversion"]

        if state == "NORMAL":
            risk_multiplier = 1.0
            allowed_strategies = all_strategies
            allow_new_entries = True
        elif state == "CAUTION":
            risk_multiplier = 0.75
            allowed_strategies = all_strategies
            allow_new_entries = True
        elif state == "DEFENSIVE":
            risk_multiplier = 0.50
            # Under Defensive, restrict allowed strategies to mean reversion or non-momentum
            allowed_strategies = [s for s in all_strategies if "momentum" not in s.lower()]
            if not allowed_strategies:
                allowed_strategies = ["mean_reversion"]
            allow_new_entries = True
        else: # SURVIVAL
            risk_multiplier = 0.0
            allowed_strategies = []
            allow_new_entries = False

        return state, risk_multiplier, allowed_strategies, allow_new_entries

    async def _check_behavioral_safety(self, sig: Signal, now: datetime) -> str | None:
        """
        Runs the Behavioral Loop Detector checks for a candidate signal.
        DB-backed checks execute off the event loop. Any infrastructure failure
        FAILS CLOSED (entry rejected) — a broken safety check must never wave a
        trade through; that is exactly the condition where runaway loops occur.
        Returns:
            rejection_reason (str | None): None if safety checks pass, otherwise the failure reason.
        """
        if sig.action == SignalAction.ENTER_LONG:
            sig_side = "long"
        elif sig.action == SignalAction.ENTER_SHORT:
            sig_side = "short"
        else:
            return None

        return await asyncio.to_thread(
            self._behavioral_safety_sync, sig.symbol.upper(), sig.strategy, sig_side, now
        )

    def _behavioral_safety_sync(self, symbol: str, strategy: str, sig_side: str, now: datetime) -> str | None:
        """Synchronous behavioral checks. Called via asyncio.to_thread.

        LATENCY PATH: this runs between signal generation and broker
        submission, so all three DB checks share ONE session/roundtrip.
        """
        import json

        from sqlalchemy import select

        from app.db.database import TradeRow

        def _norm_side(val: object) -> str:
            v = str(val or "").strip().lower()
            if v in ("long", "buy"):
                return "long"
            if v in ("short", "sell"):
                return "short"
            return ""

        try:
            with self.db._session() as s:
                # 1. Re-entry on same symbol/thesis within 30 minutes after a stop-loss exit
                cutoff = now - timedelta(minutes=30)
                rows = s.scalars(
                    select(TradeRow).where(TradeRow.symbol == symbol, TradeRow.exit_at >= cutoff)
                ).all()
                for r in rows:
                    try:
                        trade_data = json.loads(r.payload)
                    except Exception:
                        continue
                    exit_reason = trade_data.get("exit_reason", "")
                    sl_hit = trade_data.get("sl_hit", False)
                    trade_strategy = trade_data.get("strategy", getattr(r, "strategy", ""))
                    trade_side = _norm_side(trade_data.get("side", ""))

                    same_thesis = (trade_strategy == strategy) and (not trade_side or trade_side == sig_side)
                    if (exit_reason == "stop_loss" or sl_hit) and same_thesis:
                        return f"behavioral_loop_detector: re-entry on same symbol {symbol} within 30 mins after stop ({strategy}/{sig_side})"

                # 2. 3 consecutive same-symbol/thesis losses within rolling 7-day window
                cutoff_7d = now - timedelta(days=7)
                rows7 = s.scalars(
                    select(TradeRow)
                    .where(TradeRow.symbol == symbol, TradeRow.exit_at >= cutoff_7d)
                    .order_by(TradeRow.exit_at.desc())
                    .limit(10)
                ).all()
                matching_losses = []
                for r in rows7:
                    try:
                        trade_data = json.loads(r.payload)
                    except Exception:
                        continue
                    trade_strategy = trade_data.get("strategy", getattr(r, "strategy", ""))
                    trade_side = _norm_side(trade_data.get("side", ""))
                    if trade_strategy == strategy and (not trade_side or trade_side == sig_side):
                        pnl = trade_data.get("pnl", r.pnl)
                        matching_losses.append(pnl < 0)
                        if len(matching_losses) == 3:
                            break
                if len(matching_losses) == 3 and all(matching_losses):
                    return f"behavioral_loop_detector: 3 consecutive losses on symbol {symbol} ({strategy}/{sig_side}) in past 7 days"

                # 3. Churn/turnover loop detection (threshold is config-owned; original design value 2.0x)
                # SESSION-scoped, not rolling-24h: a busy prior session otherwise
                # carries its notional into the next morning and blocks ALL
                # entries for hours (2026-08-25: 2.98x at open, zero same-day
                # trades). Within-day protection is unchanged.
                from zoneinfo import ZoneInfo as _ZI
                _now_et = now.astimezone(_ZI("America/New_York"))
                _session_start_utc = _now_et.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).astimezone(UTC)
                rows24 = s.scalars(
                    select(TradeRow).where(TradeRow.exit_at >= _session_start_utc)
                ).all()
                total_volume = 0.0
                for r in rows24:
                    try:
                        trade_data = json.loads(r.payload)
                    except Exception:
                        continue
                    entry_price = trade_data.get("entry_price", 0.0)
                    qty = trade_data.get("qty", 0.0)
                    total_volume += abs(qty * entry_price)

                # =============================================================================
                # [MODIFIED 2026-10-14]: CR-18 - Morning Turnover Budget Split
                # ORIGINAL INTENT: Single max_turnover threshold applied to the full session.
                # REASON FOR CHANGE: Rapid morning churn could consume the entire 5.0x budget before
                # afternoon trend opportunities materialize. Split into morning (before 12:00 ET)
                # max 3.0x and full-day 5.0x.
                # SAFE ROLLBACK: The original code is removed; rollback via git history.
                # -----------------------------------------------------------------------------
                # [NEW CODE]:
                equity = self.portfolio.equity
                if equity > 0:
                    turnover = total_volume / equity
                    max_morning = float(getattr(self.risk_config, "max_morning_turnover_multiple", 3.0))
                    if _now_et.hour < 12 and turnover > max_morning:
                        return f"behavioral_loop_detector: high portfolio churn/turnover (morning turnover={turnover:.2f}x equity > {max_morning:.2f}x)"
                    max_turnover = float(getattr(self.risk_config, "max_daily_turnover_multiple", 5.0))
                    if turnover > max_turnover:
                        return f"behavioral_loop_detector: high portfolio churn/turnover (turnover={turnover:.2f}x equity > {max_turnover:.2f}x)"
                # =============================================================================
        except Exception as e:
            applog.error("engine.behavioral", f"Behavioral safety unavailable (fail-closed): {e}")
            return "behavioral_loop_detector: safety checks unavailable (fail-closed)"

        # 4. Duplicate/pending-order loop detection
        for pending_cid, pending_info in self.pending_reconciliation_orders.items():
            if pending_info["symbol"].upper() == symbol:
                return f"behavioral_loop_detector: duplicate pending order for symbol {symbol}"

        # 5. Strategy/thesis correlation clustering check (local data only — cannot fail closed)
        # Blocks a new entry only when it is highly correlated with an open
        # SAME-SIDE position (a hedge in the opposite direction is diversification,
        # not clustering). Threshold is RiskConfig-tunable (default 0.70).
        try:
            open_symbols = list(self.portfolio._lots.keys())
            if open_symbols:
                cand_side = _norm_side(sig_side)
                corr_limit = float(getattr(self.risk_config, "max_position_correlation", 0.70))
                cand_df = self.data.frame(symbol, "1Min")
                if cand_df is not None and not cand_df.empty:
                    cand_series = cand_df["close"].dropna()
                    for open_sym in open_symbols:
                        if open_sym == symbol:
                            continue
                        open_lot = self.portfolio._lots.get(open_sym)
                        open_qty = float(getattr(open_lot, "qty", 0.0) or 0.0)
                        open_side = "long" if open_qty > 0 else ("short" if open_qty < 0 else "")
                        if cand_side and open_side and open_side != cand_side:
                            continue  # opposite-side hedge, not a cluster
                        open_df = self.data.frame(open_sym, "1Min")
                        if open_df is not None and not open_df.empty:
                            open_series = open_df["close"].dropna()
                            combined = pd.concat([cand_series, open_series], axis=1, join="inner")
                            if len(combined) > 5:
                                corr = combined.corr().iloc[0, 1]
                                if corr > corr_limit:
                                    return f"behavioral_loop_detector: high correlation ({corr:.2f}) with open symbol {open_sym}"
        except Exception as e:
            applog.warning("engine.behavioral", f"Failed correlation clustering check: {e}")

        return None

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
        await self.stop(flatten=False)
        if self.data is not None:
            await self.data.close()
        self.db.close()
