"""Production replay runtime.

This module drives the real TradingEngine._tick coroutine. Replay supplies only
runtime dependencies (clock, data and broker); scanner, strategies, ML/meta,
portfolio allocation, Spawn shadow coordinator, risk and exits remain the live
engine implementations.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.config import Settings
from app.core.domain import EngineState
from app.core.engine import TradingEngine
from app.core.runtime import TradingRuntime
from app.db.database import Database
from app.replay.broker import ReplayBroker
from app.replay.clock import ReplayClock
from app.replay.ledger import ReplayLedger
from app.replay.market_feed import ReplayDataManager, ReplayMarketFeed
from app.replay.parity_validator import ProductionParityValidator
from app.replay.session import ReplaySessionManager
from app.core.logging import applog


class ReplayRuntime:
    def __init__(self, settings: Settings, db: Database, replay_id: str | None = None,
                 session_manager: ReplaySessionManager | None = None, data_path: Path | None = None) -> None:
        self.settings = settings
        self.source_db = db
        self.replay_id = replay_id or f"rpl_{uuid.uuid4().hex[:12]}"
        self.session_manager = session_manager or ReplaySessionManager()
        self.feed = ReplayMarketFeed(data_path=data_path)

    def run_replay(self, start_date: str, end_date: str, symbols: list[str] | None = None,
                   fill_model: str = "ohlc_touch", slippage_bps: float = 0.0,
                   verify_parity_pass: bool = True) -> dict[str, Any]:
        return asyncio.run(self.run_replay_async(start_date, end_date, symbols, fill_model, slippage_bps, verify_parity_pass))

    async def run_replay_async(self, start_date: str, end_date: str, symbols: list[str] | None = None,
                               fill_model: str = "ohlc_touch", slippage_bps: float = 0.0,
                               verify_parity_pass: bool = True) -> dict[str, Any]:
        config = {"start_date": start_date, "end_date": end_date, "symbols": symbols,
                  "fill_model": fill_model, "slippage_bps": slippage_bps}
        self.session_manager.create_session(self.replay_id, start_date, end_date, config)
        self.session_manager.update_session(self.replay_id, status="RUNNING", progress_pct=2.0)
        try:
            bars = self.feed.load_bars(start_date, end_date, symbols)
            manifest = self._build_manifest(config, bars)
            self.session_manager.update_session(self.replay_id, manifest=manifest)

            ledger_a = await self._execute_single_pass(bars, fill_model, slippage_bps, "A")
            self.session_manager.update_session(self.replay_id, progress_pct=50.0)
            ledger_b = ledger_a
            if verify_parity_pass:
                ledger_b = await self._execute_single_pass(bars, fill_model, slippage_bps, "B")
            parity = ProductionParityValidator.validate_passes(ledger_a, ledger_b)
            self._assert_meaningful_execution(ledger_a)

            summary = {
                "replay_id": self.replay_id, "status": "COMPLETED",
                "production_ticks": ledger_a.production_ticks,
                "total_candidates": len(ledger_a.candidates), "total_signals": len(ledger_a.signals),
                "total_trades": len(ledger_a.trades), "total_orders": len(ledger_a.orders),
                "total_fills": len(ledger_a.fills),
                "final_equity": ledger_a.equity_curve[-1]["equity"],
                "parity_verdict": parity["determinism_verdict"],
                "state_hash": parity["stage_matches"]["overall_state_hash"]["hash_a"],
            }
            if verify_parity_pass and not parity["overall_match"]:
                raise RuntimeError("Replay determinism failure: Pass A != Pass B")
            self.session_manager.update_session(self.replay_id, status="COMPLETED", progress_pct=100.0, summary=summary)
            return {"session": summary, "manifest": manifest, "parity": parity, "ledger": ledger_a.to_payload()}
        except Exception as exc:
            applog.error("replay_runtime", f"Replay {self.replay_id} failed closed: {exc}")
            self.session_manager.update_session(self.replay_id, status="FAILED", progress_pct=100.0,
                                                summary={"replay_id": self.replay_id, "status": "FAILED", "error": str(exc)})
            raise

    async def _execute_single_pass(self, bars: list[dict[str, Any]], fill_model: str,
                                   slippage_bps: float, pass_name: str) -> ReplayLedger:
        first_ts = bars[0]["ts_dt"]
        clock = ReplayClock(first_ts, always_open=False)
        data = ReplayDataManager(bars)
        broker = ReplayBroker(starting_cash=float(getattr(self.settings, "paper_starting_cash", 100000.0)),
                              slippage_bps=slippage_bps, fill_model=fill_model)
        ledger = ReplayLedger(f"{self.replay_id}:{pass_name}")
        broker.attach_ledger(ledger)

        with tempfile.TemporaryDirectory(prefix="alpha_replay_") as tmp:
            replay_db = self._clone_database(Path(tmp) / "replay.db")
            runtime = TradingRuntime(clock=clock, broker=broker, data=data, ledger=ledger)
            engine = TradingEngine(self.settings, replay_db, runtime=runtime)
            await self._initialize_engine(engine, clock, broker, data, ledger)

            by_ts: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
            for bar in bars:
                by_ts[bar["ts_dt"]].append(bar)

            # TradingEngine still imports utcnow as a module-level helper. Until
            # that legacy call is fully clock-injected, bind it to ReplayClock
            # for the duration of the production tick. This changes time only;
            # no decision method is replaced or bypassed.
            with ExitStack() as stack:
                stack.enter_context(patch("app.core.engine.utcnow", clock.now_utc))
                for idx, ts in enumerate(sorted(by_ts)):
                    if self.session_manager.is_cancelled(self.replay_id):
                        raise RuntimeError("Replay cancelled")
                    clock.set_time(ts)
                    data.set_time(ts)
                    for bar in sorted(by_ts[ts], key=lambda b: b["symbol"]):
                        open_px = float(bar.get("open", bar.get("o", bar.get("close", bar.get("c")))))
                        high_px = float(bar.get("high", bar.get("h", open_px)))
                        low_px = float(bar.get("low", bar.get("l", open_px)))
                        close_px = float(bar.get("close", bar.get("c", open_px)))
                        broker.process_ohlc(bar["symbol"], open_px, high_px, low_px, close_px, ts)
                    await engine._tick()
                    ledger.production_ticks += 1
                    acct = await broker.account()
                    positions = await broker.positions()
                    ledger.record_equity(ts, acct.equity, acct.cash, acct.buying_power, len(positions))
                    if idx % 100 == 0:
                        self.session_manager.update_session(self.replay_id, progress_pct=min(49.0, 5.0 + 44.0 * idx / max(1, len(by_ts))))

            await engine.spawn_coordinator.stop()
        return ledger

    async def _initialize_engine(self, engine: TradingEngine, clock: ReplayClock, broker: ReplayBroker,
                                 data: ReplayDataManager, ledger: ReplayLedger) -> None:
        """Mirror production start() initialization without replacing replay dependencies."""
        from app.portfolio.allocator import CapitalAllocationOptimizer, PortfolioAIGate
        from app.portfolio.tracker import PortfolioTracker
        from app.risk.manager import RiskManager
        from app.strategies import ParallelEvidenceEvaluator, SignalPipeline

        engine.mode = "paper"
        engine.clock, engine.data, engine.broker = clock, data, broker
        engine.runtime_ledger = ledger
        engine.risk = RiskManager(engine.risk_config, clock)
        engine.risk.core_watchlist = engine.core_watchlist
        engine.portfolio = PortfolioTracker()
        engine.portfolio_gate = PortfolioAIGate(max_correlation=engine.risk_config.max_correlation_penalty,
                                                max_positions_per_sector=engine.risk_config.max_positions_per_sector)
        engine.portfolio_allocator = CapitalAllocationOptimizer(kelly_fraction_cap=engine.risk_config.kelly_fraction_cap)
        active_snapshot = engine.db.get_setting("active_policy_snapshot")
        ml_threshold = engine.risk_config.ml_confidence_threshold
        if active_snapshot and "ml_threshold" in active_snapshot:
            ml_threshold = float(active_snapshot["ml_threshold"])
            engine.risk_config.ml_confidence_threshold = ml_threshold
        elif engine.db.get_setting("ml_threshold") is not None:
            ml_threshold = float(engine.db.get_setting("ml_threshold"))
            engine.risk_config.ml_confidence_threshold = ml_threshold
        engine.pipeline = SignalPipeline(engine.model, ml_threshold)
        engine.evidence_evaluator = ParallelEvidenceEvaluator(model=engine.model, ml_threshold=ml_threshold,
            meta_label_engine=engine.pipeline.meta_label_engine, trade_score_engine=engine.pipeline.trade_score_engine)
        for strategy in engine.strategies:
            strategy.reset()
        account = await broker.account()
        engine.portfolio.roll_session(clock.session_date(clock.now_utc()), account.equity)
        engine.portfolio.peak_equity = account.equity
        engine.portfolio.mark_equity(account.equity, clock.now_utc())
        engine.state = EngineState.RUNNING
        engine.publish = ledger.capture_publish
        await engine.spawn_coordinator.start()

    def _clone_database(self, target: Path) -> Database:
        """SQLite backup: production configuration in, replay mutations isolated out."""
        target.parent.mkdir(parents=True, exist_ok=True)
        source_conn = self.source_db.engine.raw_connection()
        import sqlite3
        dest_conn = sqlite3.connect(target)
        try:
            source_conn.connection.backup(dest_conn)
        finally:
            dest_conn.close()
            source_conn.close()
        return Database(target)

    def _build_manifest(self, config: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
        symbols = sorted({b["symbol"] for b in bars})
        data_digest = hashlib.sha256("\n".join(
            json.dumps({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in b.items() if k != "ts_dt"}, sort_keys=True, default=str)
            for b in bars).encode()).hexdigest()
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            commit = os.getenv("GIT_COMMIT_SHA", "unknown")
        return {
            "replay_id": self.replay_id, "engine_commit": commit,
            "config_hash": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
            "data_hash": data_digest, "bar_count": len(bars), "symbols": symbols,
            "first_timestamp": bars[0]["ts_dt"].isoformat(), "last_timestamp": bars[-1]["ts_dt"].isoformat(),
            "decision_path": "TradingEngine._tick", "lookahead_policy": "as_of_only",
            "database_policy": "sqlite_snapshot_isolated", "clock_policy": "ReplayClock",
        }

    @staticmethod
    def _assert_meaningful_execution(ledger: ReplayLedger) -> None:
        if ledger.production_ticks <= 0:
            raise RuntimeError("Replay executed zero production ticks")
        if not ledger.equity_curve:
            raise RuntimeError("Replay produced no portfolio states")
