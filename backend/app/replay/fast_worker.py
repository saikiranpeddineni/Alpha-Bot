"""Day-shard worker for the fast exact-path replay.

Runs in a separate process (spawn-safe: module-level entrypoint). Each shard
executes ONE trading day through the authoritative TradingEngine._tick with:
  - no ledger event recording (publish -> no-op)
  - no heartbeat ops monitors / no equity persistence (replay_fast_path)
  - no PIS profit-memory persistence
  - deterministic datetime/uuid bindings identical to the full runtime

Days are independent by construction: production EOD liquidation guarantees a
flat book at each session boundary, so shards merge losslessly.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections import defaultdict
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch


def run_day_shard(payload: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_run_day_shard_async(payload))


async def _run_day_shard_async(payload: dict[str, Any]) -> dict[str, Any]:
    from app.config import Settings, _replay_models_dir
    from app.core.domain import EngineState, _mock_utcnow
    from app.core.engine import TradingEngine
    from app.core.runtime import TradingRuntime
    from app.core.shadow_ledger import shadow_ledger
    from app.db.database import Database
    from app.replay.broker import ReplayBroker
    from app.replay.clock import ReplayClock
    from app.replay.determinism import DeterministicUUIDProxy
    from app.replay.market_feed import ReplayDataManager

    # Shard processes are short-lived; keep rejection forensics out of the
    # production shadow ledger (same isolation as the continuous runtime).
    shadow_ledger.enabled = False

    bars: list[dict[str, Any]] = payload["bars"]
    settings = Settings(**payload["settings"])
    snapshot_db = Path(payload["snapshot_db"])
    models_snapshot = Path(payload["models_snapshot"])

    replay_bars = [b for b in bars if b.get("in_replay_window")]
    if not replay_bars:
        return {"trades": [], "equity_curve": [], "ticks": 0, "flat_ok": True}

    first_ts = replay_bars[0]["ts_dt"]
    clock = ReplayClock(first_ts, always_open=False)
    data = ReplayDataManager(bars)
    broker = ReplayBroker(
        float(getattr(settings, "paper_starting_cash", 100000.0)),
        float(payload.get("slippage_bps", 0.0)),
        payload.get("fill_model", "ohlc_touch"),
        volume_constrained=bool(payload.get("volume_constrained", False)),
        max_volume_pct=float(payload.get("max_volume_pct", 0.05)),
    )

    tmp = Path(tempfile.mkdtemp(prefix="alpha_shard_"))
    replay_db: Database | None = None
    engine: TradingEngine | None = None
    trades_out: list[dict[str, Any]] = []
    equity_out: list[list[Any]] = []
    ticks = 0
    try:
        rp_db = tmp / "replay.db"
        shutil.copy2(snapshot_db, rp_db)
        replay_db = Database(rp_db)
        with replay_db.engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=MEMORY")
            conn.exec_driver_sql("PRAGMA synchronous=OFF")
            conn.exec_driver_sql("PRAGMA temp_store=MEMORY")
            for table in ("orders", "trades", "equity_snapshots"):
                try:
                    conn.exec_driver_sql(f"DELETE FROM {table}")
                except Exception:
                    pass
            conn.exec_driver_sql("DELETE FROM app_settings WHERE key IN ('pending_reconciliation_orders','heartbeat_write_check')")

        replay_models = tmp / "models"
        shutil.copytree(models_snapshot, replay_models, dirs_exist_ok=True)

        _replay_models_dir.set(replay_models)
        _mock_utcnow.set(clock.now_utc)

        engine = TradingEngine(settings, replay_db, runtime=TradingRuntime(clock=clock, broker=broker, data=data))
        engine.replay_fast_path = True
        engine._skip_broadcast = True
        if hasattr(engine, "pis_adapter"):
            engine.pis_adapter.persist_profit_memory = False

        # ---- lean initialization (no ledger wrappers, no publish sink) ----
        from app.portfolio.allocator import CapitalAllocationOptimizer, PortfolioAIGate
        from app.portfolio.tracker import PortfolioTracker
        from app.risk.manager import RiskManager
        from app.strategies import SignalPipeline

        engine.mode = "paper"
        engine.clock, engine.data, engine.broker = clock, data, broker
        engine.risk = RiskManager(engine.risk_config, clock)
        engine.risk.core_watchlist = engine.core_watchlist
        engine.portfolio = PortfolioTracker()
        engine.portfolio_gate = PortfolioAIGate(
            max_correlation=engine.risk_config.max_correlation_penalty,
            max_positions_per_sector=engine.risk_config.max_positions_per_sector,
        )
        engine.portfolio_allocator = CapitalAllocationOptimizer(kelly_fraction_cap=engine.risk_config.kelly_fraction_cap)
        ml = float(engine.db.get_setting("ml_threshold") or engine.risk_config.ml_confidence_threshold)
        engine.risk_config.ml_confidence_threshold = ml
        engine.pipeline = SignalPipeline(engine.model, ml, risk_config=engine.risk_config, settings=engine.settings)
        for strategy in engine.strategies:
            strategy.reset()
        account = await broker.account()
        engine.portfolio.roll_session(clock.session_date(clock.now_utc()), account.equity)
        engine.portfolio.peak_equity = account.equity
        engine.portfolio.mark_equity(account.equity, clock.now_utc())
        engine.state = EngineState.RUNNING
        engine.publish = lambda *_a, **_k: None

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

        deterministic_uuid = DeterministicUUIDProxy(f"{payload['day']}|shard")
        with ExitStack() as stack:
            stack.enter_context(patch("app.core.engine.datetime", ContextAwareDateTime))
            stack.enter_context(patch("app.core.scanner.datetime", ContextAwareDateTime))
            stack.enter_context(patch("app.risk.manager.datetime", ContextAwareDateTime))
            stack.enter_context(patch("app.core.domain.uuid", deterministic_uuid))

            eq_every = 30
            for idx, ts in enumerate(sorted(by_ts)):
                clock.set_time(ts)
                data.set_time(ts)
                ts_bars = by_ts[ts]
                is_window = any(b.get("in_replay_window", False) for b in ts_bars)
                for bar in sorted(ts_bars, key=lambda b: b["symbol"]):
                    close = float(bar.get("close", bar.get("c")))
                    vol = float(bar.get("volume", bar.get("v", 0.0)))
                    broker.process_ohlc(
                        bar["symbol"],
                        float(bar.get("open", bar.get("o", close))),
                        float(bar.get("high", bar.get("h", close))),
                        float(bar.get("low", bar.get("l", close))),
                        close, ts, volume=vol,
                    )
                engine.allow_new_entries = is_window
                # _force_gated survives _check_system_heartbeat's recompute of
                # allow_new_entries; without it warmup history would admit entries.
                engine._force_gated = not is_window
                await engine._tick()
                if is_window:
                    ticks += 1
                    if ticks % eq_every == 0:
                        acct = await broker.account()
                        equity_out.append([ts.isoformat(), acct.equity])

        positions_left = await broker.positions()
        flat_ok = len(positions_left) == 0

        with replay_db.engine.connect() as conn:
            rows = conn.exec_driver_sql("SELECT exit_at, symbol, payload FROM trades ORDER BY exit_at, id").fetchall()
        for exit_at, symbol, pl in rows:
            try:
                details = __import__("json").loads(pl)
            except Exception:
                continue
            ts = exit_at if isinstance(exit_at, datetime) else datetime.fromisoformat(str(exit_at))
            details.setdefault("exit_at", ts.isoformat())
            details.setdefault("symbol", symbol)
            trades_out.append(details)

        return {"trades": trades_out, "equity_curve": equity_out, "ticks": ticks, "flat_ok": flat_ok}
    finally:
        import app.db.database as db_module
        db_module._ACTIVE_DATABASE = None
        if replay_db is not None:
            try:
                replay_db.engine.dispose(close=True)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
