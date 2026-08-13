"""Production replay runtime driving the authoritative TradingEngine._tick path."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time as _wall_time
import traceback
import uuid
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.ai.model import SignalModel
from app.config import MODELS_DIR, Settings
from app.core.applog import applog
from app.core.domain import EngineState
from app.core.engine import TradingEngine
from app.core.runtime import TradingRuntime
from app.db.database import Database
from app.replay.broker import ReplayBroker
from app.replay.clock import ReplayClock
from app.replay.determinism import DeterministicUUIDProxy
from app.replay.ledger import ReplayLedger
from app.replay.market_feed import ReplayDataManager, ReplayMarketFeed
from app.replay.parity_validator import ProductionParityValidator
from app.replay.session import ReplaySessionManager


# Production market-risk parity requires substantially more history than the
# strategy warmup.  The market-regime SPY/QQQ family looks back 60 calendar
# days and ranks the latest ATR percentile over as many as 200 hourly bars.
# Keep that context available without forcing every symbol to carry 60 days
# of minute bars.
_GENERAL_CONTEXT_DAYS = 10
_RISK_CONTEXT_DAYS = 65
_MIN_GENERAL_WARMUP_BARS = 1600
_RISK_CONTEXT_BARS = 26000
_RISK_CONTEXT_SYMBOLS = {"SPY", "QQQ"}
_REPLAY_DATA_MAX_ROWS = 30000


class ReplayRuntime:
    def __init__(self, settings: Settings, db: Database, replay_id: str | None = None,
                 session_manager: ReplaySessionManager | None = None, data_path: Path | None = None) -> None:
        self.settings, self.source_db = settings, db
        self.replay_id = replay_id or f"rpl_{uuid.uuid4().hex[:12]}"
        self.session_manager = session_manager or ReplaySessionManager()
        self.feed = ReplayMarketFeed(data_path=data_path)

    def run_replay(self, start_date: str, end_date: str, symbols: list[str] | None = None,
                   fill_model: str = "ohlc_touch", slippage_bps: float = 0.0, verify_parity_pass: bool = True,
                   volume_constrained: bool = False, max_volume_pct: float = 0.05, warmup_bars: int = 0) -> dict[str, Any]:
        return asyncio.run(self.run_replay_async(start_date, end_date, symbols, fill_model, slippage_bps, verify_parity_pass, volume_constrained, max_volume_pct, warmup_bars))

    async def run_replay_async(self, start_date: str, end_date: str, symbols: list[str] | None = None,
                               fill_model: str = "ohlc_touch", slippage_bps: float = 0.0, verify_parity_pass: bool = True,
                               volume_constrained: bool = False, max_volume_pct: float = 0.05, warmup_bars: int = 0) -> dict[str, Any]:
        effective_warmup_bars = max(int(warmup_bars or 0), _MIN_GENERAL_WARMUP_BARS)
        config = {
            "start_date": start_date,
            "end_date": end_date,
            "symbols": symbols,
            "fill_model": fill_model,
            "slippage_bps": slippage_bps,
            "volume_constrained": volume_constrained,
            "max_volume_pct": max_volume_pct,
            "warmup_bars_requested": int(warmup_bars or 0),
            "warmup_bars_effective": effective_warmup_bars,
            "risk_context_days": _RISK_CONTEXT_DAYS,
            "verify_parity_pass": verify_parity_pass,
        }
        if not self.session_manager.get_session(self.replay_id):
            self.session_manager.create_session(self.replay_id, start_date, end_date, config)
        self.session_manager.update_session(self.replay_id, status="RUNNING", progress_pct=2.0)
        
        # Flush initial skeleton ledger so the UI doesn't return 404 while downloading bars
        self.session_manager.save_artifact(self.replay_id, "ledger", {"timeline": [], "production_ticks": 0, "candidates": []})
        self.session_manager.save_artifact(self.replay_id, "timeline", [])

        run_snapshot = Path(tempfile.mkdtemp(prefix="alpha_replay_run_snapshot_"))
        try:
            if symbols is not None:
                target_symbols = list({s.upper() for s in symbols})
            else:
                base_watchlist = self.source_db.get_setting("watchlist") or self.settings.watchlist_symbols()
                live_symbols = set()
                try:
                    c = self.source_db.conn.cursor()
                    c.execute("SELECT symbol, payload FROM trades")
                    for r in c.fetchall():
                        sym, payload = r[0], r[1]
                        if payload:
                            p = json.loads(payload)
                            entry_at = p.get("entry_at", "")
                            if start_date <= entry_at <= end_date + "T23:59:59":
                                live_symbols.add(sym)
                except Exception:
                    pass
                target_symbols = list({s.upper() for s in (set(base_watchlist) | live_symbols)})

            # Market-risk parity is mandatory even when the user explicitly replays
            # a narrow symbol list.  SPY/QQQ/VIX are reference inputs, not trade
            # candidates, and are therefore always present in the replay data set.
            target_symbols = sorted(set(target_symbols) | {"SPY", "QQQ", "VIX"})

            # Download a short general context for breadth/universe calculations,
            # then a longer context only for the SPY/QQQ volatility families.  The
            # requested replay window itself remains start_date..end_date.
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            general_context_start = (start_dt - timedelta(days=_GENERAL_CONTEXT_DAYS)).strftime("%Y-%m-%d")
            risk_context_start = (start_dt - timedelta(days=_RISK_CONTEXT_DAYS)).strftime("%Y-%m-%d")
            await self.feed.ensure_data(general_context_start, end_date, target_symbols, settings=self.settings)
            await self.feed.ensure_data(risk_context_start, end_date, ["SPY", "QQQ"], settings=self.settings)

            bars = self.feed.load_bars(start_date, end_date, target_symbols)
            filtered_bars = []
            by_sym = defaultdict(list)
            for b in bars:
                by_sym[b["symbol"]].append(b)
            for symbol, sym_bars in by_sym.items():
                sym_bars.sort(key=lambda x: x["ts_dt"])
                first_in_window = next((i for i, b in enumerate(sym_bars) if b["in_replay_window"]), len(sym_bars))
                if symbol.upper() in _RISK_CONTEXT_SYMBOLS:
                    symbol_warmup = _RISK_CONTEXT_BARS
                else:
                    symbol_warmup = effective_warmup_bars
                start_idx = max(0, first_in_window - symbol_warmup)
                filtered_bars.extend(sym_bars[start_idx:])
            bars = sorted(filtered_bars, key=lambda b: (b["ts_dt"], b["symbol"]))
            replay_bars = [b for b in bars if b.get("in_replay_window", True)]
            if not replay_bars:
                raise RuntimeError("No bars in requested replay window")

            first_active_bar = replay_bars[0]["ts_dt"]
            model_asof = first_active_bar
            
            ET = ZoneInfo("America/New_York")
            
            market_open = first_active_bar.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = first_active_bar.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
            
            # Hard assertions for Model-as-of and Replay window
            if first_active_bar != market_open:
                raise RuntimeError(f"first_active_bar {first_active_bar} != market open {market_open}")
            if model_asof != first_active_bar:
                raise RuntimeError(f"model_asof {model_asof} != first_active_bar {first_active_bar}")
            if model_asof < market_open:
                raise RuntimeError(f"model_asof {model_asof} is pre-market")
            
            applog.info("replay.runtime", f"Diagnostic Clock Bounds: requested_start={start_date}")
            applog.info("replay.runtime", f"Diagnostic Clock Bounds: requested_end={end_date}")
            applog.info("replay.runtime", f"Diagnostic Clock Bounds: market_open_et={market_open.isoformat()}")
            applog.info("replay.runtime", f"Diagnostic Clock Bounds: market_close_et={market_close.isoformat()}")
            applog.info("replay.runtime", f"Diagnostic Clock Bounds: first_warmup_timestamp={bars[0]['ts_dt'].isoformat()}")
            applog.info("replay.runtime", f"Diagnostic Clock Bounds: first_active_timestamp={first_active_bar.isoformat()}")
            applog.info("replay.runtime", f"Diagnostic Clock Bounds: model_asof={model_asof.isoformat()}")
            applog.info("replay.runtime", f"Diagnostic Clock Bounds: last_active_timestamp={replay_bars[-1]['ts_dt'].isoformat()}")
            
            snapshot_db = run_snapshot / "source.db"
            self._snapshot_source_database(snapshot_db)
            snapshot_models = run_snapshot / "models"
            archived_loaded = False
            if model_asof:
                asof_dt = model_asof if model_asof.tzinfo else model_asof.replace(tzinfo=timezone.utc)
                ET = ZoneInfo("America/New_York")
                archive_dir = MODELS_DIR / "archive" / asof_dt.astimezone(ET).strftime("%Y-%m-%d")
                if archive_dir.exists():
                    shutil.copytree(archive_dir, snapshot_models, dirs_exist_ok=True)
                    archived_loaded = True
            if not archived_loaded:
                shutil.copytree(MODELS_DIR, snapshot_models, dirs_exist_ok=True)
            snapshot_models.mkdir(parents=True, exist_ok=True)
            model_asof_report = self._enforce_model_asof(snapshot_models, model_asof)
            snapshot_fingerprint = self._snapshot_fingerprint(snapshot_db, snapshot_models)
            manifest = self._build_manifest(config, bars)
            manifest.update({
                "run_snapshot_fingerprint": snapshot_fingerprint,
                "database_policy": "single_immutable_run_snapshot_cloned_per_pass",
                "model_policy": "immutable_asof_snapshot_cloned_per_pass",
                "model_asof": model_asof.isoformat(),
                "model_asof_report": model_asof_report,
                "production_path": "TradingEngine._tick",
                "replay_isolation": "ReplayClock+ReplayBroker+cloned DB/models; shadow replay disabled; warmup execution fail-closed",
                "market_risk_context": {
                    "general_context_start": general_context_start,
                    "risk_context_start": risk_context_start,
                    "risk_symbols": sorted(_RISK_CONTEXT_SYMBOLS),
                    "risk_context_bars_per_symbol": _RISK_CONTEXT_BARS,
                },
            })
            self.session_manager.update_session(self.replay_id, manifest=manifest)

            a = await self._execute_single_pass(bars, fill_model, slippage_bps, "A", snapshot_db, snapshot_models, volume_constrained, max_volume_pct, effective_warmup_bars)
            self._assert_meaningful_execution(a)
            parity = None
            if verify_parity_pass:
                self.session_manager.update_session(self.replay_id, progress_pct=50.0)
                b = await self._execute_single_pass(bars, fill_model, slippage_bps, "B", snapshot_db, snapshot_models, volume_constrained, max_volume_pct, effective_warmup_bars)
                self._assert_meaningful_execution(b)
                parity = ProductionParityValidator.validate_passes(a, b)
                if not parity["overall_match"]:
                    raise RuntimeError(f"Replay production-path determinism failed: {parity.get('first_divergence')}")

            self.session_manager.update_session(self.replay_id, progress_pct=100.0)
            summary = {
                "replay_id": self.replay_id,
                "status": "COMPLETED",
                "production_ticks": a.production_ticks,
                "total_candidates": len(a.candidates),
                "total_signals": len(a.signals),
                "total_trades": len(a.trades),
                "total_orders": len(a.orders),
                "total_fills": len(a.fills),
                "final_equity": a.equity_curve[-1]["equity"] if a.equity_curve else 100000.0,
                "parity_verdict": parity["determinism_verdict"] if parity else "NOT_REQUESTED",
                "state_hash": parity["stage_matches"]["overall_state_hash"]["hash_a"] if parity else "N/A",
                "stage_matches": parity["stage_matches"] if parity else {},
                "first_divergence": parity["first_divergence"] if parity else None,
            }
            self.session_manager.update_session(self.replay_id, status="COMPLETED", progress_pct=100.0, summary=summary)
            return {"session": summary, "manifest": manifest, "parity": parity, "ledger": a.to_payload()}
        except Exception as exc:
            applog.error("replay_runtime", f"Replay {self.replay_id} failed closed: {exc}")
            failed_summary = {"replay_id": self.replay_id, "status": "FAILED", "error": str(exc)}
            self.session_manager.update_session(self.replay_id, status="FAILED", progress_pct=100.0, summary=failed_summary)
            raise
        finally:
            shutil.rmtree(run_snapshot, ignore_errors=True)

    async def _execute_single_pass(self, bars: list[dict[str, Any]], fill_model: str, slippage_bps: float,
                                   pass_name: str, snapshot_db: Path, snapshot_models: Path,
                                   volume_constrained: bool = False, max_volume_pct: float = 0.05, warmup_bars: int = 0) -> ReplayLedger:
        replay_bars = [b for b in bars if b.get("in_replay_window", True)]
        if not replay_bars:
            raise RuntimeError("No bars in requested replay window")
        first_ts = replay_bars[0]["ts_dt"]
        clock = ReplayClock(first_ts, always_open=False)
        data = ReplayDataManager(bars, max_rows=_REPLAY_DATA_MAX_ROWS)
        broker = ReplayBroker(float(getattr(self.settings, "paper_starting_cash", 100000.0)), slippage_bps, fill_model,
                              volume_constrained=volume_constrained, max_volume_pct=max_volume_pct)
        start_date_str = replay_bars[0]["ts_dt"].strftime("%Y-%m-%d")
        end_date_str = replay_bars[-1]["ts_dt"].strftime("%Y-%m-%d")
        ledger = ReplayLedger(f"{self.replay_id}:{pass_name}", start_date=start_date_str, end_date=end_date_str)
        broker.attach_ledger(ledger)
        
        # Write real initial skeleton ledger incrementally
        payload = ledger.to_payload()
        self.session_manager.save_artifact(self.replay_id, "ledger", payload)
        self.session_manager.save_artifact(self.replay_id, "timeline", payload.get("timeline", []))
        
        tmp_path = Path(tempfile.mkdtemp(prefix="alpha_replay_"))
        replay_db: Database | None = None
        engine: TradingEngine | None = None
        model_token = utcnow_token = None
        previous_active_db = None
        try:
            replay_db_path = tmp_path / "replay.db"
            shutil.copy2(snapshot_db, replay_db_path)
            replay_db = Database(replay_db_path)
            self._ensure_replay_schema_parity(replay_db)
            self._clear_mutable_replay_tables(replay_db)
            replay_models = tmp_path / "models"
            shutil.copytree(snapshot_models, replay_models, dirs_exist_ok=True)
            from app.config import _replay_models_dir
            from app.core.domain import _mock_utcnow
            model_token = _replay_models_dir.set(replay_models)
            utcnow_token = _mock_utcnow.set(clock.now_utc)

            import app.db.database as db_module
            previous_active_db = db_module._ACTIVE_DATABASE
            db_module._ACTIVE_DATABASE = replay_db

            engine = TradingEngine(self.settings, replay_db, runtime=TradingRuntime(clock=clock, broker=broker, data=data, ledger=ledger))
            engine.model = SignalModel(replay_models, risk_config=engine.risk_config)
            await self._initialize_engine(engine, clock, broker, data, ledger, replay_models)
            by_ts: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
            for bar in bars:
                by_ts[bar["ts_dt"]].append(bar)

            class ContextAwareDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    mock = _mock_utcnow.get()
                    if mock is not None:
                        value = mock()
                        return value.astimezone(tz) if tz else value.replace(tzinfo=None)
                    return datetime.now(tz)

            deterministic_uuid = DeterministicUUIDProxy(
                f"{self.replay_id.split(':',1)[0]}|{replay_bars[0]['ts_dt'].isoformat()}|{replay_bars[-1]['ts_dt'].isoformat()}"
            )
            # --- REPLAY DIAGNOSTIC INSTRUMENTATION ---
            # Task 1: Track the last active stage for watchdog/stall diagnosis.
            _diag_stage = "pre_loop"
            _diag_first_tick_done = False
            _diag_tick_wall_start: float | None = None

            # Task 2: Disable gap/movers scanners during replay — these are live-only
            # utilities that hit Alpaca's screener/snapshot endpoints and have no
            # historical replay equivalent.  Disabling them here does NOT affect any
            # production code path; it only applies inside this ExitStack scope.
            async def _replay_noop_gap_movers() -> list[str]:
                applog.info("replay.runtime", f"[diag] replay_id={self.replay_id} stage=gap_movers_skipped (replay-only noop)")
                return []

            with ExitStack() as stack:
                stack.enter_context(patch("app.core.engine.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.core.scanner.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.risk.manager.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.core.shadow_ledger.uuid", deterministic_uuid))
                stack.enter_context(patch("app.core.domain.uuid", deterministic_uuid))
                stack.enter_context(patch("app.strategies.refresh_contextual_memory", lambda _db: None))
                stack.enter_context(patch("app.strategies.memory.refresh_contextual_memory", lambda _db: None))
                # Task 2: Suppress live-market discovery sources that make network calls.
                # PreMarketGapScanner.get_gap_movers() hits Alpaca snapshot / screener APIs.
                # TopMoversScanner._fetch_movers() hits Alpaca screener or downloads bars.
                # Neither makes sense during a historical replay — the replay data set is
                # bounded and known; injecting live movers would be lookahead contamination.
                stack.enter_context(patch(
                    "app.core.gap_scanner.PreMarketGapScanner.get_gap_movers",
                    lambda self_: _replay_noop_gap_movers()
                ))
                from app.core.top_movers_scanner import TopMoversResult
                async def _replay_noop_top_movers(_self) -> TopMoversResult:
                    applog.info("replay.runtime", f"[diag] replay_id={self.replay_id} stage=top_movers_skipped (replay-only noop)")
                    return TopMoversResult(source="replay_noop")
                stack.enter_context(patch(
                    "app.core.top_movers_scanner.TopMoversScanner._fetch_movers",
                    _replay_noop_top_movers
                ))

                for idx, ts in enumerate(sorted(by_ts)):
                    if self.session_manager.is_cancelled(self.replay_id):
                        raise RuntimeError("Replay cancelled")

                    # Task 1: stage boundary logging (every tick during first 10 in-window ticks;
                    # then every 50 ticks so logs stay usable at scale).
                    _is_replay_tick = any(b.get("in_replay_window", True) for b in by_ts[ts])
                    _verbose_diag = (ledger.production_ticks < 10) or (idx % 50 == 0)

                    if _verbose_diag:
                        applog.info("replay.runtime", (
                            f"[diag] replay_id={self.replay_id} pass={pass_name} "
                            f"tick={idx} sim_ts={ts.isoformat()} "
                            f"wall={datetime.now(timezone.utc).isoformat()} "
                            f"is_replay_window={_is_replay_tick} "
                            f"production_ticks={ledger.production_ticks}"
                        ))

                    _diag_stage = "clock_set"
                    clock.set_time(ts)
                    data.set_time(ts)
                    ts_bars = by_ts[ts]
                    is_replay_window = any(b.get("in_replay_window", True) for b in ts_bars)

                    _diag_stage = "broker_ohlc"
                    for bar in sorted(ts_bars, key=lambda b: b["symbol"]):
                        close = float(bar.get("close", bar.get("c")))
                        vol = float(bar.get("volume", bar.get("v", 0.0)))
                        broker.process_ohlc(bar["symbol"], float(bar.get("open", bar.get("o", close))),
                                            float(bar.get("high", bar.get("h", close))), float(bar.get("low", bar.get("l", close))),
                                            close, ts, volume=vol)

                    # Warmup builds indicators/state but must never execute the
                    # production scheduler, scanner, event monitor, or entries.
                    desired_state = EngineState.RUNNING if is_replay_window else EngineState.SAFE_MODE
                    engine.state = desired_state
                    engine.allow_new_entries = is_replay_window

                    # Task 3: Watchdog for first-tick stall.
                    _diag_stage = "engine_tick"
                    if is_replay_window and not _diag_first_tick_done:
                        applog.info("replay.runtime", (
                            f"[diag] replay_id={self.replay_id} FIRST_PRODUCTION_TICK_START "
                            f"sim_ts={ts.isoformat()} wall={datetime.now(timezone.utc).isoformat()}"
                        ))
                        _diag_tick_wall_start = _wall_time.monotonic()

                    await engine._tick()

                    if is_replay_window and not _diag_first_tick_done:
                        _diag_first_tick_done = True
                        elapsed = _wall_time.monotonic() - (_diag_tick_wall_start or 0)
                        applog.info("replay.runtime", (
                            f"[diag] replay_id={self.replay_id} FIRST_PRODUCTION_TICK_DONE "
                            f"elapsed_wall={elapsed:.2f}s sim_ts={ts.isoformat()}"
                        ))
                        if elapsed > 10.0:
                            applog.warning("replay.runtime", (
                                f"[diag] replay_id={self.replay_id} FIRST_TICK_SLOW "
                                f"elapsed={elapsed:.2f}s — possible stall detected. "
                                f"Last active stage: {_diag_stage}"
                            ))

                    _diag_stage = "await_aux_tasks"
                    await self._await_replay_aux_tasks(engine)

                    _diag_stage = "post_tick"
                    if engine.state == EngineState.HALTED:
                        raise RuntimeError(f"Replay engine halted at {ts.isoformat()}: {getattr(engine.risk, 'hard_halt_reason', '')}")
                    if is_replay_window:
                        ledger.production_ticks += 1
                        acct, positions = await broker.account(), await broker.positions()
                        ledger.record_equity(ts, acct.equity, acct.cash, acct.buying_power, len(positions))

                    # Task 4: Heartbeat granularity — every 10 ticks in the first 200,
                    # then every 100 ticks.  This ensures the first production tick is
                    # always visible in the session record without the 100-tick blind spot.
                    _heartbeat_interval = 10 if idx < 200 else 100
                    if idx % _heartbeat_interval == 0:
                        pct = min(49.0, 5.0 + 44.0 * idx / max(1, len(by_ts))) if pass_name == "A" else min(100.0, 50.0 + 50.0 * idx / max(1, len(by_ts)))
                        self.session_manager.update_session(self.replay_id, progress_pct=pct)
                        self.session_manager.update_heartbeat(self.replay_id)
                        # Always flush artifacts incrementally so the UI sees warmup progression
                        payload = ledger.to_payload()
                        self.session_manager.save_artifact(self.replay_id, "ledger", payload)
                        self.session_manager.save_artifact(self.replay_id, "timeline", payload.get("timeline", []))
            await self._stop_replay_engine(engine)
            self._hydrate_db_ledger(replay_db, ledger)
            return ledger
        finally:
            import app.db.database as db_module
            if engine is not None:
                await self._stop_replay_engine(engine)
            if replay_db is not None:
                replay_db.engine.dispose(close=True)
            db_module._ACTIVE_DATABASE = previous_active_db
            if utcnow_token is not None:
                from app.core.domain import _mock_utcnow
                _mock_utcnow.reset(utcnow_token)
            if model_token is not None:
                from app.config import _replay_models_dir
                _replay_models_dir.reset(model_token)
            shutil.rmtree(tmp_path, ignore_errors=False)

    @staticmethod
    async def _await_replay_aux_tasks(engine: TradingEngine) -> None:
        """Make replay-side detached production jobs deterministic.

        Live trading deliberately detaches scanner/event-monitor work so it cannot
        block the market tick. Replay has no latency SLA, so the same jobs are awaited
        before advancing simulated time. Shadow replay is explicitly disabled by
        _initialize_engine because it is a research feedback loop, not production
        decision logic.
        """
        for attr in ("_scanner_task", "_event_monitor_task"):
            task = getattr(engine, attr, None)
            if task is None:
                continue
            if not task.done():
                await task
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                raise exc

    @staticmethod
    async def _stop_replay_engine(engine: TradingEngine) -> None:
        for attr in ("_scanner_task", "_event_monitor_task", "_shadow_replay_task", "_trainer_task"):
            task = getattr(engine, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            setattr(engine, attr, None)

    async def _initialize_engine(self, engine: TradingEngine, clock: ReplayClock, broker: ReplayBroker, data: ReplayDataManager, ledger: ReplayLedger, replay_models: Path) -> None:
        from app.portfolio.allocator import CapitalAllocationOptimizer, PortfolioAIGate
        from app.portfolio.tracker import PortfolioTracker
        from app.risk.manager import RiskManager
        from app.strategies import ParallelEvidenceEvaluator, SignalPipeline
        engine.mode = "paper"
        engine.clock, engine.data, engine.broker, engine.runtime_ledger = clock, data, broker, ledger
        engine.risk = RiskManager(engine.risk_config, clock)
        engine.risk.core_watchlist = engine.core_watchlist
        engine.portfolio = PortfolioTracker()
        engine.portfolio_gate = PortfolioAIGate(max_correlation=engine.risk_config.max_correlation_penalty, max_positions_per_sector=engine.risk_config.max_positions_per_sector)
        engine.portfolio_allocator = CapitalAllocationOptimizer(kelly_fraction_cap=engine.risk_config.kelly_fraction_cap)
        active = engine.db.get_setting("active_policy_snapshot")
        ml = float(active["ml_threshold"]) if active and "ml_threshold" in active else float(engine.db.get_setting("ml_threshold") or engine.risk_config.ml_confidence_threshold)
        engine.risk_config.ml_confidence_threshold = ml
        engine.pipeline = SignalPipeline(engine.model, ml, risk_config=engine.risk_config, settings=engine.settings)
        engine.evidence_evaluator = ParallelEvidenceEvaluator(model=engine.model, ml_threshold=ml, meta_label_engine=engine.pipeline.meta_label_engine, trade_score_engine=engine.pipeline.trade_score_engine)

        # Shadow replay is a live/research feedback loop. Running it inside a
        # deterministic production replay creates exactly the contamination seen in
        # the Aug-04 logs: rejected signals generated by the replay are immediately
        # replayed again at simulated time +60m. Disable it for this isolated pass.
        async def disabled_shadow_replay(_now):
            return None
        engine._replay_pending_signals = disabled_shadow_replay

        for strategy in engine.strategies:
            strategy.reset()
            original_on_bar = strategy.on_bar

            def observed_on_bar(ctx, symbol, df, _orig=original_on_bar, _name=strategy.name):
                sig = _orig(ctx, symbol, df)
                if sig is not None and (not ledger.start_date or clock.now_utc().strftime("%Y-%m-%d") >= ledger.start_date):
                    details = sig.model_dump(mode="json") if hasattr(sig, "model_dump") else {"strategy": _name, "action": str(getattr(sig, "action", "")), "price": getattr(sig, "price", None)}
                    ledger.record_event("STRATEGY_SIGNAL", clock.now_utc(), str(getattr(sig, "symbol", symbol)), details)
                return sig
            strategy.on_bar = observed_on_bar

        original_evaluate_entry = engine.risk.evaluate_entry

        def observed_evaluate_entry(signal, ctx, bars, now=None, kelly_override=None):
            decision = original_evaluate_entry(signal, ctx, bars, now=now, kelly_override=kelly_override)
            if not ledger.start_date or (now or clock.now_utc()).strftime("%Y-%m-%d") >= ledger.start_date:
                details = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else {"allowed": getattr(decision, "allowed", None), "reason": getattr(decision, "reason", None), "qty": getattr(decision, "qty", None)}
                details["candidate_id"] = getattr(signal, "candidate_id", None)
                details["strategy"] = getattr(signal, "strategy", None)
                ledger.record_event("RISK_DECISION", now or clock.now_utc(), str(getattr(signal, "symbol", "")), details)
            return decision

        engine.risk.evaluate_entry = observed_evaluate_entry
        account = await broker.account()
        engine.portfolio.roll_session(clock.session_date(clock.now_utc()), account.equity)
        engine.portfolio.peak_equity = account.equity
        engine.portfolio.mark_equity(account.equity, clock.now_utc())
        engine.state = EngineState.RUNNING
        engine.publish = ledger.capture_publish

    def _snapshot_source_database(self, target: Path) -> None:
        import sqlite3
        source = self.source_db.engine.raw_connection()
        dest = sqlite3.connect(target)
        try:
            driver = getattr(source, "driver_connection", None) or getattr(source, "connection", source)
            driver.backup(dest)
        finally:
            dest.close()
            source.close()

    @staticmethod
    def _enforce_model_asof(models_path: Path, cutoff: datetime) -> dict[str, Any]:
        cutoff_utc = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
        cutoff_epoch = cutoff_utc.timestamp()
        kept: list[str] = []
        excluded: list[dict[str, str]] = []
        for path in sorted(p for p in models_path.rglob("*") if p.is_file()):
            rel = str(path.relative_to(models_path)).replace("\\", "/")
            mtime = path.stat().st_mtime
            if mtime > cutoff_epoch:
                excluded.append({"path": rel, "available_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat()})
                path.unlink()
            else:
                kept.append(rel)
        for directory in sorted((p for p in models_path.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        applog.info("replay_runtime", f"Model as-of {cutoff_utc.isoformat()}: kept={len(kept)} excluded_future={len(excluded)}")
        return {"cutoff": cutoff_utc.isoformat(), "kept_count": len(kept), "excluded_future_count": len(excluded), "excluded_future": excluded}

    @staticmethod
    def _snapshot_fingerprint(db_path: Path, models_path: Path) -> str:
        h = hashlib.sha256()
        h.update(db_path.read_bytes())
        if models_path.exists():
            for path in sorted(p for p in models_path.rglob("*") if p.is_file()):
                h.update(str(path.relative_to(models_path)).encode())
                h.update(path.read_bytes())
        return h.hexdigest()

    @staticmethod
    def _ensure_replay_schema_parity(db: Database) -> None:
        pass

    @staticmethod
    def _clear_mutable_replay_tables(db: Database) -> None:
        with db.engine.begin() as conn:
            for table in ("orders", "trades", "equity_snapshots", "shadow_ledger"):
                try:
                    conn.exec_driver_sql(f"DELETE FROM {table}")
                except Exception:
                    pass
            conn.exec_driver_sql("DELETE FROM app_settings WHERE key IN ('pending_reconciliation_orders','heartbeat_write_check')")

    @staticmethod
    def _hydrate_db_ledger(db: Database, ledger: ReplayLedger) -> None:
        start_date = getattr(ledger, "start_date", None)
        with db.engine.connect() as conn:
            for row in conn.exec_driver_sql("SELECT ts,symbol,payload FROM shadow_ledger ORDER BY ts,id").fetchall():
                try:
                    payload = json.loads(row[2])
                except Exception:
                    continue
                ts = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]))
                if start_date and ts.strftime("%Y-%m-%d") < start_date:
                    continue
                ledger.record_event("SCANNER_CANDIDATE", ts, row[1], payload)
                evidence = payload.get("gate_evidence_json") or {}
                if isinstance(evidence, str):
                    try:
                        evidence = json.loads(evidence)
                    except Exception:
                        evidence = {}
                ledger.record_event("ML_DECISION", ts, row[1], {"ml_probability":payload.get("ml_probability"),"ml_gate_decision":payload.get("ml_gate_decision"),"meta_probability":payload.get("meta_probability"),"trade_score":payload.get("trade_score"),"gate_evidence":evidence})
            for row in conn.exec_driver_sql("SELECT exit_at,symbol,payload FROM trades ORDER BY exit_at,id").fetchall():
                try:
                    details = json.loads(row[2])
                except Exception:
                    continue
                ts = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]))
                if start_date and ts.strftime("%Y-%m-%d") < start_date:
                    continue
                ledger.record_event("TRADE_CLOSED", ts, row[1], details)

    def _build_manifest(self, config: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
        digest = hashlib.sha256("\n".join(json.dumps({k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in b.items() if k!="ts_dt"}, sort_keys=True, default=str) for b in bars).encode()).hexdigest()
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            commit = os.getenv("GIT_COMMIT_SHA", "unknown")
        window = [b for b in bars if b.get("in_replay_window", True)]
        syms = sorted({b["symbol"] for b in bars})
        return {
            "replay_id": self.replay_id,
            "engine_commit": commit,
            "config_hash": hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
            "data_hash": digest,
            "bar_count_total": len(bars),
            "bar_count_replay_window": len(window),
            "symbols": syms,
            "first_timestamp": bars[0]["ts_dt"].isoformat(),
            "last_timestamp": bars[-1]["ts_dt"].isoformat(),
            "decision_path": "TradingEngine._tick",
            "lookahead_policy": "strict_as_of",
            "database_policy": "single_immutable_run_snapshot_cloned_per_pass",
            "model_policy": "immutable_asof_snapshot_cloned_per_pass",
            "clock_policy": "ReplayClock + engine/scanner/risk datetime binding",
            "identity_policy": "deterministic replay-only UUID binding",
            "context_memory_policy": "immutable_run_start_snapshot_no_replay_feedback",
            "market_risk_policy": "historical SPY+QQQ+VIX required with 60-day SPY/QQQ context",
            "strategy_observation_policy": "replay-only wrapper around production on_bar",
            "risk_observation_policy": "replay-only wrapper around production evaluate_entry",
            "fill_model": config["fill_model"],
            "slippage_bps": config["slippage_bps"],
        }

    @staticmethod
    def _assert_meaningful_execution(ledger: ReplayLedger) -> None:
        if ledger.production_ticks <= 0:
            raise RuntimeError("Replay executed zero production ticks")
        if not ledger.equity_curve:
            raise RuntimeError("Replay produced no portfolio states")
