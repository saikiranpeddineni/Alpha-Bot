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
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone, UTC
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd

from app.ai.insights import InsightEngine
from app.research.event_builder import candidate_event_from_signal, market_bar_event
from app.research.shadow_runtime import ShadowResearchRuntime
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
    ProtectionState,
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
from app.pis.engine_cutover import PISLiveExitController
from app.pis.telemetry import evaluation_payload
from app.pis.staleness import assess_evidence_freshness
from app.pis.session_context import session_context

ET = ZoneInfo("America/New_York")

MAX_CONSECUTIVE_TICK_ERRORS = 30
EQUITY_PERSIST_EVERY_TICKS = 12
METRICS_BROADCAST_EVERY_TICKS = 6
_DATA_GAP_THRESHOLD_SECS = 120.0

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
        # One explicit production-to-research boundary. Research receives only
        # immutable value snapshots and has no execution-result channel.
        self.shadow_research = ShadowResearchRuntime(
            db=self.db,
            gemini_api_key=settings.gemini_api_key,
            gemini_observation_sink=self._persist_gemini_shadow_observation,
        )
        self._regimes: dict[str, Regime] = {}
        self._position_peaks: dict[str, float] = {}
        self._position_troughs: dict[str, float] = {}
        self._position_initial_stops: dict[str, float | None] = {}
        # Original strategy target survives broker protection repair/reconciliation.
        self._position_take_profits: dict[str, float] = {}
        # PR #271 V2 Adaptive Harvesting: Isolated Shadow State Store (NON-AUTHORITATIVE)
        self._v2_shadow_states: dict[str, dict] = {}
        # LIVE exit ownership.  This is deliberately separate from V2 shadow.
        # A symbol enters this set only after Alpaca confirms BOTH the extended TP
        # and a non-loss/profit-lock stop.  Until then all legacy exit safety nets
        # remain authoritative.
        self._live_runner_symbols: set[str] = set()
        self._live_runner_floor: dict[str, float] = {}
        self._pis_exit = PISLiveExitController()
        self._last_scan_date = None
        self._last_train_date = None
        self._last_event_monitor_min = None
        self._event_monitor_task: asyncio.Task | None = None
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
        self._last_heartbeat_week: int | None = None
        # fix(#45): in-flight flatten guard — deduplicate concurrent flatten for same symbol
        self._flattening_symbols: set[str] = set()
        # fix(#56): retain emergency flatten task reference for cancellation on stop()
        self._emergency_flatten_task: asyncio.Task | None = None
        # fix(#86): track market-data stale timestamps per symbol
        self._last_data_bar_ts: dict[str, datetime] = {}
        self._data_gap_symbols: set[str] = set()

        # Slow scheduled work is explicitly detached from the market-data/execution tick.
        self._scanner_task: asyncio.Task | None = None
        self._trainer_task: asyncio.Task | None = None
        self._shadow_replay_task: asyncio.Task | None = None

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
            broker_name = "alpaca" if (self.settings.broker == "alpaca") else self.settings.broker
        else:
            broker_name = self.settings.broker

        self.broker = make_broker(broker_name, self.settings)
        self.risk = RiskManager(self.db, self.broker, self.risk_config)
        self.portfolio = PortfolioTracker(self.db, self.broker, self.settings, self.risk)
        self.pipeline = SignalPipeline(self.model, self.db, self.settings)
        self._restore_halt_if_persisted(mode)
        self.state = EngineState.RUNNING
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self.state = EngineState.STOPPED
        if self._task is not None:
            self._task.cancel()
        if self._scanner_task is not None:
            self._scanner_task.cancel()
        if self._trainer_task is not None:
            self._trainer_task.cancel()
        if self._shadow_replay_task is not None:
            self._shadow_replay_task.cancel()
        if self._emergency_flatten_task is not None:
            self._emergency_flatten_task.cancel()
        if self.data is not None:
            await self.data.close()

    async def _close_position_guarded(self, symbol: str, reason: str) -> None:
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
            order = await self.broker.close_position(symbol, reason=reason)
            # If close failed immediately (None returned), release the guard so retries work
            if order is None:
                self._pending_close_symbols.discard(symbol)
        finally:
            self._flattening_symbols.discard(symbol)

    async def _drain_fills(self, now: datetime) -> None:
        assert self.broker and self.portfolio and self.risk
        fills = self.broker.drain_fills()
        if not fills:
            return
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
                order = self.db.get_order(fill.order_id)

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

            try:
                trade = self.portfolio.on_fill(
                    fill, peak_price=peak, trough_price=trough, initial_stop=init_stop,
                    risk_manager=self.risk, now=now
                )
            except Exception:
                trade = None
            if trade is not None:
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

    async def _manage_live_winner_exit(self, symbol: str, position: Any, df: pd.DataFrame) -> bool:
        """PIS owns live winner management. Legacy runner promotion is now a counterfactual baseline only."""
        if self.mode != "live" or self.broker is None or len(df) < 2:
            return symbol in self._live_runner_symbols
        replace_leg = getattr(self.broker, "replace_exit_leg", None)
        if replace_leg is None:
            return False
        lot = self.portfolio._lots.get(symbol) if self.portfolio else None
        initial_stop = getattr(lot, "initial_stop_loss", None) if lot is not None else None
        if initial_stop is None:
            initial_stop = self._position_initial_stops.get(symbol)
        original_tp = self._position_take_profits.get(symbol)
        entry = float(position.avg_entry_price)
        if initial_stop is None or original_tp is None:
            return symbol in self._live_runner_symbols
        risk = abs(entry - float(initial_stop))
        if risk <= 1e-8:
            return False
        side = 1.0 if position.qty > 0 else -1.0
        target_r = max(0.1, side * (float(original_tp) - entry) / risk)

        # Completed-bar causal evidence only.  Never promote from an unconfirmed
        # transient tick/wick.  Activation scales with the strategy's own target
        # rather than replacing strategy economics with one magic dollar value.
        prior = df.iloc[-2]
        if position.qty > 0:
            prior_best = float(prior["high"])
            self._position_peaks[symbol] = max(self._position_peaks.get(symbol, entry), prior_best)
            mfe_r = max(0.0, (self._position_peaks[symbol] - entry) / risk)
        else:
            prior_best = float(prior["low"])
            self._position_peaks[symbol] = min(self._position_peaks.get(symbol, entry), prior_best)
            mfe_r = max(0.0, (entry - self._position_peaks[symbol]) / risk)
        activation_r = min(1.0, max(0.75, target_r * 0.60))
        if mfe_r < activation_r:
            return symbol in self._live_runner_symbols

        # Give a newly proven winner room, then tighten progressively.  The allowed
        # giveback shrinks as a fraction of MFE while retaining at least 0.60R of
        # breathing room so ordinary one-minute noise does not dominate exits.
        if position.qty > 0:
            floor_r = max(0.0, mfe_r - max(0.60, mfe_r * 0.45))
            floor_px = entry + floor_r * risk
            floor_px = max(floor_px, entry + 0.15 * risk)
            floor_px = min(floor_px, float(df["close"].iloc[-1]) + 0.15 * risk)
            floor_px = round(floor_px, 4)
            current_floor = self._live_runner_floor.get(symbol)
            if current_floor is None or floor_px > current_floor + 1e-8:
                self._live_runner_floor[symbol] = floor_px
                try:
                    await replace_leg(symbol, stop_loss=floor_px, take_profit=None)
                    applog.info("engine.exit", f"{symbol}: live runner floor raised to {floor_px:.4f} (PIS counterfactual legacy runner now suppressed)")
                except Exception as exc:
                    applog.warning("engine.exit", f"{symbol}: live runner floor update failed: {exc}")
                    return symbol in self._live_runner_symbols
        else:
            floor_r = max(0.0, mfe_r - max(0.60, mfe_r * 0.45))
            floor_px = entry - floor_r * risk
            floor_px = min(floor_px, entry - 0.15 * risk)
            floor_px = max(floor_px, float(df["close"].iloc[-1]) - 0.15 * risk)
            floor_px = round(floor_px, 4)
            current_floor = self._live_runner_floor.get(symbol)
            if current_floor is None or floor_px < current_floor - 1e-8:
                self._live_runner_floor[symbol] = floor_px
                try:
                    await replace_leg(symbol, stop_loss=floor_px, take_profit=None)
                    applog.info("engine.exit", f"{symbol}: live runner floor lowered to {floor_px:.4f} (PIS counterfactual legacy runner now suppressed)")
                except Exception as exc:
                    applog.warning("engine.exit", f"{symbol}: live runner floor update failed: {exc}")
                    return symbol in self._live_runner_symbols

        self._live_runner_symbols.add(symbol)
        return True

    async def _evaluate_pis_for_position(self, symbol: str, position: Any, df: pd.DataFrame, now: datetime) -> None:
        if self.broker is None or self.portfolio is None:
            return
        init_stop = self._position_initial_stops.get(symbol)
        if init_stop is None:
            lot = self.portfolio._lots.get(symbol)
            init_stop = getattr(lot, "initial_stop_loss", None) if lot is not None else None
        if init_stop is None:
            init_stop = getattr(position, "stop_loss", None)
        if init_stop is None:
            return
        fresh = assess_evidence_freshness(evidence_time=df.index[-2].to_pydatetime() if len(df) >= 2 else now, now=now)
        if not fresh.fresh:
            applog.warning("engine.pis", f"{symbol}: PIS skipped due to stale evidence ({fresh.age_seconds:.1f}s)")
            return
        position_risk = abs(float(position.avg_entry_price) - float(init_stop))
        if position_risk <= 1e-8:
            applog.warning("engine.pis", f"{symbol}: PIS skipped due to invalid initial stop / risk")
            return
        rs_score: float | None = None
        try:
            spy_df = enrich(self.data.frame("SPY", "1Min", limit=600)) if self.data is not None else None
            if spy_df is not None and not spy_df.empty:
                rs_score = compute_relative_strength(df, spy_df)
        except Exception:
            rs_score = None
        market_ctx = None
        try:
            regime = detect_regime(df)
            market_ctx = 80.0 if getattr(regime, "name", str(regime)) not in {"STRESS", "CRITICAL"} else 30.0
        except Exception:
            market_ctx = None
        evaluation = self._pis_exit.evaluate(
            symbol=symbol,
            position=position,
            df=df,
            now=now,
            initial_stop=init_stop,
            relative_strength=rs_score,
            market_context=market_ctx,
        )
        self.publish("pis_decision", evaluation_payload(evaluation))
        intent = evaluation.intent
        if intent.close_position:
            await self._close_position_guarded(symbol, reason=f"PIS EXIT: {evaluation.decision.reason_codes[0] if evaluation.decision.reason_codes else 'confirmed'}")
            self._live_runner_symbols.discard(symbol)
            self._live_runner_floor.pop(symbol, None)
            self._position_peaks.pop(symbol, None)
            self._position_troughs.pop(symbol, None)
            self._position_initial_stops.pop(symbol, None)
            self._position_take_profits.pop(symbol, None)
            return
        if intent.protection is not None and self.broker is not None:
            floor = intent.protection.floor_r
            if floor is not None:
                entry = float(position.avg_entry_price)
                risk = abs(entry - float(init_stop))
                if risk > 1e-8:
                    if position.qty > 0:
                        stop_px = entry + floor * risk
                    else:
                        stop_px = entry - floor * risk
                    replace_leg = getattr(self.broker, "replace_exit_leg", None)
                    if replace_leg is not None:
                        try:
                            await replace_leg(symbol, stop_loss=round(stop_px, 4), take_profit=None)
                        except Exception as exc:
                            applog.warning("engine.pis", f"{symbol}: failed to apply PIS protection floor: {exc}")

    # broadcast payloads and remaining engine methods remain unchanged below
