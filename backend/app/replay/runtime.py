"""Production replay runtime driving the authoritative TradingEngine._tick path."""
from __future__ import annotations
import asyncio, hashlib, json, os, shutil, subprocess, tempfile, uuid
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.ai.model import SignalModel
from app.config import MODELS_DIR, Settings
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
from app.core.logging import applog


class ReplayRuntime:
    def __init__(self, settings: Settings, db: Database, replay_id: str | None = None,
                 session_manager: ReplaySessionManager | None = None, data_path: Path | None = None) -> None:
        self.settings, self.source_db = settings, db
        self.replay_id = replay_id or f"rpl_{uuid.uuid4().hex[:12]}"
        self.session_manager = session_manager or ReplaySessionManager()
        self.feed = ReplayMarketFeed(data_path=data_path)

    def run_replay(self, start_date: str, end_date: str, symbols: list[str] | None = None,
                   fill_model: str = "ohlc_touch", slippage_bps: float = 0.0, verify_parity_pass: bool = True) -> dict[str, Any]:
        return asyncio.run(self.run_replay_async(start_date, end_date, symbols, fill_model, slippage_bps, verify_parity_pass))

    async def run_replay_async(self, start_date: str, end_date: str, symbols: list[str] | None = None,
                               fill_model: str = "ohlc_touch", slippage_bps: float = 0.0, verify_parity_pass: bool = True) -> dict[str, Any]:
        config = {"start_date": start_date, "end_date": end_date, "symbols": symbols, "fill_model": fill_model, "slippage_bps": slippage_bps}
        self.session_manager.create_session(self.replay_id, start_date, end_date, config)
        self.session_manager.update_session(self.replay_id, status="RUNNING", progress_pct=2.0)
        parity: dict[str, Any] | None = None
        run_snapshot = Path(tempfile.mkdtemp(prefix="alpha_replay_run_snapshot_"))
        try:
            # Fallback to DB watchlist or core watchlist if symbols is None
            target_symbols = symbols if symbols is not None else (self.source_db.get_setting("watchlist") or self.settings.watchlist_symbols())
            await self.feed.ensure_data(start_date, end_date, target_symbols)
            
            bars = self.feed.load_bars(start_date, end_date, symbols)
            replay_bars = [b for b in bars if b.get("in_replay_window", True)]
            if not replay_bars:
                raise RuntimeError("No bars in requested replay window")
            model_asof = replay_bars[0]["ts_dt"]
            snapshot_db = run_snapshot / "source.db"
            self._snapshot_source_database(snapshot_db)
            snapshot_models = run_snapshot / "models"
            if MODELS_DIR.exists():
                shutil.copytree(MODELS_DIR, snapshot_models, dirs_exist_ok=True)
            else:
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
            })
            self.session_manager.update_session(self.replay_id, manifest=manifest)
            a = await self._execute_single_pass(bars, fill_model, slippage_bps, "A", snapshot_db, snapshot_models)
            self.session_manager.update_session(self.replay_id, progress_pct=100.0)
            self._assert_meaningful_execution(a)
            
            summary = {"replay_id": self.replay_id, "status": "COMPLETED", "production_ticks": a.production_ticks,
                       "total_candidates": len(a.candidates), "total_signals": len(a.signals), "total_trades": len(a.trades),
                       "total_orders": len(a.orders), "total_fills": len(a.fills), "final_equity": a.equity_curve[-1]["equity"],
                       "parity_verdict": "SKIPPED", "state_hash": "N/A",
                       "stage_matches": {}, "first_divergence": None}
            self.session_manager.update_session(self.replay_id, status="COMPLETED", progress_pct=100.0, summary=summary)
            return {"session": summary, "manifest": manifest, "parity": None, "ledger": a.to_payload()}
        except Exception as exc:
            applog.error("replay_runtime", f"Replay {self.replay_id} failed closed: {exc}")
            failed_summary = {"replay_id": self.replay_id, "status": "FAILED", "error": str(exc)}
            self.session_manager.update_session(self.replay_id, status="FAILED", progress_pct=100.0, summary=failed_summary)
            raise
        finally:
            shutil.rmtree(run_snapshot, ignore_errors=True)

    async def _execute_single_pass(self, bars: list[dict[str, Any]], fill_model: str, slippage_bps: float,
                                   pass_name: str, snapshot_db: Path, snapshot_models: Path) -> ReplayLedger:
        replay_bars = [b for b in bars if b.get("in_replay_window", True)]
        if not replay_bars:
            raise RuntimeError("No bars in requested replay window")
        first_ts = replay_bars[0]["ts_dt"]
        clock, data = ReplayClock(first_ts, always_open=False), ReplayDataManager(bars)
        broker = ReplayBroker(float(getattr(self.settings, "paper_starting_cash", 100000.0)), slippage_bps, fill_model)
        ledger = ReplayLedger(f"{self.replay_id}:{pass_name}"); broker.attach_ledger(ledger)
        tmp_path = Path(tempfile.mkdtemp(prefix="alpha_replay_"))
        replay_db: Database | None = None
        engine: TradingEngine | None = None
        try:
            replay_db_path = tmp_path / "replay.db"
            shutil.copy2(snapshot_db, replay_db_path)
            replay_db = Database(replay_db_path)
            self._ensure_replay_schema_parity(replay_db)
            self._clear_mutable_replay_tables(replay_db)
            replay_models = tmp_path / "models"
            shutil.copytree(snapshot_models, replay_models, dirs_exist_ok=True)
            # TradingEngine and SignalPipeline import MODELS_DIR as production globals.
            # Bind those globals only while constructing replay components so every ML,
            # meta-label and trainer reference resolves to the historical as-of snapshot.
            from app.config import _replay_models_dir
            from app.core.domain import _mock_utcnow
            _replay_models_dir.set(replay_models)
            _mock_utcnow.set(clock.now_utc)
            
            if True:
                engine = TradingEngine(self.settings, replay_db, runtime=TradingRuntime(clock=clock, broker=broker, data=data, ledger=ledger))
                engine.model = SignalModel(replay_models)
                await self._initialize_engine(engine, clock, broker, data, ledger, replay_models)
            by_ts: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
            for bar in replay_bars: by_ts[bar["ts_dt"]].append(bar)

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
            with ExitStack() as stack:
                stack.enter_context(patch("app.core.engine.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.core.scanner.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.risk.manager.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.core.shadow_ledger.uuid", deterministic_uuid))
                stack.enter_context(patch("app.core.domain.uuid", deterministic_uuid))
                stack.enter_context(patch("app.strategies.refresh_contextual_memory", lambda _db: None))
                stack.enter_context(patch("app.strategies.memory.refresh_contextual_memory", lambda _db: None))
                for idx, ts in enumerate(sorted(by_ts)):
                    if self.session_manager.is_cancelled(self.replay_id): raise RuntimeError("Replay cancelled")
                    clock.set_time(ts); data.set_time(ts)
                    for bar in sorted(by_ts[ts], key=lambda b: b["symbol"]):
                        close = float(bar.get("close", bar.get("c")))
                        broker.process_ohlc(bar["symbol"], float(bar.get("open", bar.get("o", close))), float(bar.get("high", bar.get("h", close))), float(bar.get("low", bar.get("l", close))), close, ts)
                    await engine._tick()
                    spawn = getattr(getattr(engine, "shadow_research", None), "spawn", None)
                    if spawn is not None and spawn.running:
                        await spawn.queue.join()
                    ledger.production_ticks += 1
                    acct, positions = await broker.account(), await broker.positions()
                    ledger.record_equity(ts, acct.equity, acct.cash, acct.buying_power, len(positions))
                    if idx % 100 == 0:
                        self.session_manager.update_session(self.replay_id, progress_pct=min(49.0, 5.0 + 44.0 * idx / max(1, len(by_ts))))
            await self._stop_replay_engine(engine)
            self._hydrate_db_ledger(replay_db, ledger)
            return ledger
        finally:
            import app.db.database as db_module
            db_module._ACTIVE_DATABASE = self.source_db
            if engine is not None: await self._stop_replay_engine(engine)
            if replay_db is not None: replay_db.engine.dispose(close=True)
            shutil.rmtree(tmp_path, ignore_errors=False)

    @staticmethod
    async def _stop_replay_engine(engine: TradingEngine) -> None:
        coordinator = getattr(engine, "spawn_coordinator", None)
        if coordinator is not None and getattr(coordinator, "running", False): await coordinator.stop()
        await asyncio.sleep(0)

    async def _initialize_engine(self, engine: TradingEngine, clock: ReplayClock, broker: ReplayBroker, data: ReplayDataManager, ledger: ReplayLedger, replay_models: Path) -> None:
        from app.portfolio.allocator import CapitalAllocationOptimizer, PortfolioAIGate
        from app.portfolio.tracker import PortfolioTracker
        from app.risk.manager import RiskManager
        from app.strategies import ParallelEvidenceEvaluator, SignalPipeline
        engine.mode = "paper"; engine.clock, engine.data, engine.broker, engine.runtime_ledger = clock, data, broker, ledger
        engine.risk = RiskManager(engine.risk_config, clock); engine.risk.core_watchlist = engine.core_watchlist
        engine.portfolio = PortfolioTracker()
        engine.portfolio_gate = PortfolioAIGate(max_correlation=engine.risk_config.max_correlation_penalty, max_positions_per_sector=engine.risk_config.max_positions_per_sector)
        engine.portfolio_allocator = CapitalAllocationOptimizer(kelly_fraction_cap=engine.risk_config.kelly_fraction_cap)
        active = engine.db.get_setting("active_policy_snapshot")
        ml = float(active["ml_threshold"]) if active and "ml_threshold" in active else float(engine.db.get_setting("ml_threshold") or engine.risk_config.ml_confidence_threshold)
        engine.risk_config.ml_confidence_threshold = ml
        engine.pipeline = SignalPipeline(engine.model, ml)
        engine.evidence_evaluator = ParallelEvidenceEvaluator(model=engine.model, ml_threshold=ml, meta_label_engine=engine.pipeline.meta_label_engine, trade_score_engine=engine.pipeline.trade_score_engine)

        for strategy in engine.strategies:
            strategy.reset()
            original_on_bar = strategy.on_bar
            def observed_on_bar(ctx, symbol, df, _orig=original_on_bar, _name=strategy.name):
                sig = _orig(ctx, symbol, df)
                if sig is not None:
                    details = sig.model_dump(mode="json") if hasattr(sig, "model_dump") else {"strategy": _name, "action": str(getattr(sig, "action", "")), "price": getattr(sig, "price", None)}
                    ledger.record_event("STRATEGY_SIGNAL", clock.now_utc(), str(getattr(sig, "symbol", symbol)), details)
                return sig
            strategy.on_bar = observed_on_bar

        original_evaluate_entry = engine.risk.evaluate_entry
        def observed_evaluate_entry(signal, ctx, bars, now=None, kelly_override=None):
            decision = original_evaluate_entry(signal, ctx, bars, now=now, kelly_override=kelly_override)
            details = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else {"allowed": getattr(decision, "allowed", None), "reason": getattr(decision, "reason", None), "qty": getattr(decision, "qty", None)}
            details["candidate_id"] = getattr(signal, "candidate_id", None)
            details["strategy"] = getattr(signal, "strategy", None)
            ledger.record_event("RISK_DECISION", now or clock.now_utc(), str(getattr(signal, "symbol", "")), details)
            return decision
        engine.risk.evaluate_entry = observed_evaluate_entry

        account = await broker.account(); engine.portfolio.roll_session(clock.session_date(clock.now_utc()), account.equity)
        engine.portfolio.peak_equity = account.equity; engine.portfolio.mark_equity(account.equity, clock.now_utc())
        engine.state = EngineState.RUNNING; engine.publish = ledger.capture_publish
        await engine.shadow_research.start()

    def _snapshot_source_database(self, target: Path) -> None:
        """Take one transactionally consistent DB snapshot for the entire A/B replay run."""
        import sqlite3
        source = self.source_db.engine.raw_connection(); dest = sqlite3.connect(target)
        try:
            driver = getattr(source, "driver_connection", None) or getattr(source, "connection", source)
            driver.backup(dest)
        finally:
            dest.close(); source.close()

    @staticmethod
    def _enforce_model_asof(models_path: Path, cutoff: datetime) -> dict[str, Any]:
        """Remove replay-visible artifacts that did not exist at the replay start.

        This is replay-only isolation. Production model loading is untouched. File mtime is
        used as the conservative availability boundary because legacy specialist/adaptive
        artifacts do not all carry a machine-readable trained_at manifest. copytree/copy2
        preserve mtimes, so the boundary survives creation of the immutable run snapshot.
        """
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
        # Empty future-generation/candidate directories are irrelevant and can confuse
        # artifact discovery/debugging, so remove only empty directories from the snapshot.
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
        required_columns = {"spawned_strategies": {"parent_policy_id":"VARCHAR(64)","policy_version":"VARCHAR(20)","config_snapshot_json":"TEXT","config_fingerprint":"VARCHAR(64)","created_from_candidate":"VARCHAR(64)","spawn_reason":"TEXT","execution_mode":"VARCHAR(20)","code_commit_sha":"VARCHAR(40)","model_manifest_sha":"VARCHAR(64)"}}
        with db.engine.begin() as conn:
            for table, columns in required_columns.items():
                existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
                if not existing: raise RuntimeError(f"Replay database missing required production table: {table}")
                for name, sql_type in columns.items():
                    if name not in existing: conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    @staticmethod
    def _clear_mutable_replay_tables(db: Database) -> None:
        with db.engine.begin() as conn:
            for table in ("orders","trades","equity_snapshots","shadow_ledger","spawn_shadow_events"): conn.exec_driver_sql(f"DELETE FROM {table}")
            conn.exec_driver_sql("DELETE FROM app_settings WHERE key IN ('pending_reconciliation_orders','heartbeat_write_check')")

    @staticmethod
    def _hydrate_db_ledger(db: Database, ledger: ReplayLedger) -> None:
        with db.engine.connect() as conn:
            for row in conn.exec_driver_sql("SELECT ts,symbol,payload FROM shadow_ledger ORDER BY ts,id").fetchall():
                try: payload = json.loads(row[2])
                except Exception: continue
                ts = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]))
                ledger.record_event("SCANNER_CANDIDATE", ts, row[1], payload)
                evidence = payload.get("gate_evidence_json") or {}
                if isinstance(evidence, str):
                    try: evidence = json.loads(evidence)
                    except Exception: evidence = {}
                ledger.record_event("ML_DECISION", ts, row[1], {"ml_probability":payload.get("ml_probability"),"ml_gate_decision":payload.get("ml_gate_decision"),"meta_probability":payload.get("meta_probability"),"trade_score":payload.get("trade_score"),"gate_evidence":evidence})
            for row in conn.exec_driver_sql("SELECT ts,symbol,payload_json FROM spawn_shadow_events ORDER BY ts,id").fetchall():
                try: details = json.loads(row[2]) if row[2] else {}
                except Exception: details = {}
                ts = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0])); ledger.record_event("SPAWN_DECISION", ts, row[1], details)
            for row in conn.exec_driver_sql("SELECT exit_at,symbol,payload FROM trades ORDER BY exit_at,id").fetchall():
                try: details = json.loads(row[2])
                except Exception: continue
                ts = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0])); ledger.record_event("TRADE_CLOSED", ts, row[1], details)

    def _build_manifest(self, config: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
        digest = hashlib.sha256("\n".join(json.dumps({k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in b.items() if k!="ts_dt"},sort_keys=True,default=str) for b in bars).encode()).hexdigest()
        try: commit = subprocess.check_output(["git","rev-parse","HEAD"],text=True,stderr=subprocess.DEVNULL).strip()
        except Exception: commit = os.getenv("GIT_COMMIT_SHA","unknown")
        window=[b for b in bars if b.get("in_replay_window",True)]; syms=sorted({b["symbol"] for b in bars})
        return {"replay_id":self.replay_id,"engine_commit":commit,"config_hash":hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),"data_hash":digest,"bar_count_total":len(bars),"bar_count_replay_window":len(window),"symbols":syms,"first_timestamp":bars[0]["ts_dt"].isoformat(),"last_timestamp":bars[-1]["ts_dt"].isoformat(),"decision_path":"TradingEngine._tick","lookahead_policy":"strict_as_of","database_policy":"single_immutable_run_snapshot_cloned_per_pass","model_policy":"immutable_asof_snapshot_cloned_per_pass","spawn_policy":"production_coordinator_queue_drained_per_timestamp","clock_policy":"ReplayClock + engine/scanner/risk datetime binding","identity_policy":"deterministic replay-only UUID binding","context_memory_policy":"immutable_run_start_snapshot_no_replay_feedback","market_risk_policy":"historical SPY+QQQ+VIX required; no UNKNOWN substitution","strategy_observation_policy":"replay-only wrapper around production on_bar","risk_observation_policy":"replay-only wrapper around production evaluate_entry","fill_model":config["fill_model"],"slippage_bps":config["slippage_bps"]}

    @staticmethod
    def _assert_meaningful_execution(ledger: ReplayLedger) -> None:
        if ledger.production_ticks <= 0: raise RuntimeError("Replay executed zero production ticks")
        if not ledger.equity_curve: raise RuntimeError("Replay produced no portfolio states")
