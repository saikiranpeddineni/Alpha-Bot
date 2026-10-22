"""Production replay runtime driving the authoritative TradingEngine._tick path."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections import defaultdict
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
from app.replay.session import ReplaySessionManager


class ReplayRuntime:
    def __init__(self, settings: Settings, db: Database, replay_id: str | None = None,
                 session_manager: ReplaySessionManager | None = None, data_path: Path | None = None) -> None:
        self.settings, self.source_db = settings, db
        self.replay_id = replay_id or f"rpl_{uuid.uuid4().hex[:12]}"
        self.session_manager = session_manager or ReplaySessionManager()
        self.feed = ReplayMarketFeed(data_path=data_path)

    @staticmethod
    def _expand_dynamic_scanner_symbols(
        symbols: list[str],
        enabled: bool,
    ) -> list[str]:
        """Return the causal symbol surface required by fast replay scanning."""
        normalized = {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
        if enabled:
            from app.core.scanner import CANDIDATE_POOL

            normalized.update(CANDIDATE_POOL)
        return sorted(normalized)

    def run_replay(self, start_date: str, end_date: str, symbols: list[str] | None = None,
                   fill_model: str = "ohlc_touch", slippage_bps: float = 0.0, verify_parity_pass: bool = True,
                   volume_constrained: bool = False, max_volume_pct: float = 0.05, warmup_bars: int = 0) -> dict[str, Any]:
        return asyncio.run(self.run_replay_async(start_date, end_date, symbols, fill_model, slippage_bps, verify_parity_pass, volume_constrained, max_volume_pct, warmup_bars))

    # ------------------------------------------------------------------
    # FAST EXACT-PATH REPLAY (Option A)
    # Lean prod-tick execution: no ledger wrappers, no heartbeat ops monitors,
    # no PIS memory persistence. Days are INDEPENDENT (EOD flatten guarantee),
    # so shards run across processes and merge losslessly.
    # ------------------------------------------------------------------
    # =============================================================================
    # [MODIFIED 2026-10-14]: L50 - Dynamic Starting Cash Passthrough in Replay Runtime
    # ORIGINAL INTENT: run_replay_fast_async did not accept starting_cash, defaulting to fixed setting.
    # REASON FOR CHANGE: Caller starting_cash was ignored by replay engine.
    # SAFE ROLLBACK: The original code is removed; rollback via git history.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    async def run_replay_fast_async(self, start_date: str, end_date: str,
                                    symbols: list[str] | None = None,
                                    fill_model: str = "ohlc_touch",
                                    slippage_bps: float = 0.0,
                                     volume_constrained: bool = False,
                                     max_volume_pct: float = 0.05,
                                     warmup_bars: int = 600,
                                     starting_cash: float | None = None,
                                     use_dynamic_scanner: bool = False,
                                     max_workers: int | None = None) -> dict[str, Any]:
        import os
        from concurrent.futures import ProcessPoolExecutor
        from datetime import timedelta as _td

        from app.replay.fast_worker import run_day_shard

        config = {"start_date": start_date, "end_date": end_date, "symbols": symbols,
                  "fill_model": fill_model, "slippage_bps": slippage_bps,
                   "volume_constrained": volume_constrained, "max_volume_pct": max_volume_pct,
                   "warmup_bars": warmup_bars, "starting_cash": starting_cash,
                   "use_dynamic_scanner": use_dynamic_scanner,
                   "commission_per_share": float(getattr(self.settings, "paper_commission_per_share", 0.0)),
                  "minimum_commission": float(getattr(self.settings, "paper_minimum_commission", 0.0)),
                  "engine": "replay_fast"}
    # =============================================================================
        self.session_manager.create_session(self.replay_id, start_date, end_date, config)
        self.session_manager.update_session(self.replay_id, status="RUNNING", progress_pct=1.0)

        run_snapshot = Path(tempfile.mkdtemp(prefix="alpha_replay_fast_"))
        try:
            if symbols:
                target_symbols = [s.upper() for s in symbols]
            else:
                target_symbols = [s.upper() for s in
                                  (self.source_db.get_setting("watchlist") or self.settings.watchlist_symbols())]

            # The scanner's historical surface is the static candidate pool.
            # Load that pool into the replay input so dynamic scanner work can
            # discover causal candidates instead of seeing only the user's
            # initial watchlist plus forced SPY/QQQ/VIX context bars.
            target_symbols = self._expand_dynamic_scanner_symbols(
                target_symbols,
                use_dynamic_scanner,
            )

            from datetime import timedelta
            history_start = start_date if self.feed._explicit_data_path else (datetime.fromisoformat(start_date).date() - timedelta(days=60)).isoformat()
            await self.feed.ensure_data(history_start, end_date, target_symbols)
            bars = self.feed.load_bars(history_start, end_date, target_symbols)
            for bar in bars:
                bar["in_replay_window"] = start_date <= bar["ts_dt"].strftime("%Y-%m-%d") <= end_date

            # per-symbol warmup prefix + window filter (identical semantics to full runtime)
            by_sym = defaultdict(list)
            for b in bars:
                by_sym[b["symbol"]].append(b)
            filtered = []
            for sym, sb in by_sym.items():
                sb.sort(key=lambda x: x["ts_dt"])
                first_w = next((i for i, b in enumerate(sb) if b["in_replay_window"]), len(sb))
                start_idx = 0 if sym in {"SPY", "QQQ", "VIX", "^VIX"} else max(0, first_w - int(warmup_bars))
                filtered.extend(sb[start_idx:])
            bars = sorted(filtered, key=lambda b: (b["ts_dt"], b["symbol"]))
            window_bars = [b for b in bars if b.get("in_replay_window")]
            if not window_bars:
                raise RuntimeError("No bars in requested replay window")

            # ---- immutable snapshots prepared ONCE ----
            snapshot_db = run_snapshot / "source.db"
            self._snapshot_source_database(snapshot_db)
            models_snapshot = run_snapshot / "models"
            model_asof = window_bars[-1]["ts_dt"]
            archived = False
            from zoneinfo import ZoneInfo as _ZI
            asof_date = model_asof.astimezone(_ZI("America/New_York")).strftime("%Y-%m-%d")
            archive_dir = MODELS_DIR / "archive" / asof_date
            if archive_dir.exists():
                for item in archive_dir.iterdir():
                    dst = models_snapshot / item.name
                    if item.is_dir():
                        shutil.copytree(item, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, dst)
                archived = True
            if not archived:
                IGNORE = shutil.ignore_patterns("archive", "generations", ".specialist-candidate-*")
                shutil.copytree(MODELS_DIR, models_snapshot, ignore=IGNORE, dirs_exist_ok=True)
            models_snapshot.mkdir(parents=True, exist_ok=True)
            self._enforce_model_asof(models_snapshot, model_asof)

            settings_dump = self.settings.model_dump()

            # ---- shard by trading DAY (independent: EOD flatten guarantees flat start) ----
            days = sorted({b["ts_dt"].strftime("%Y-%m-%d") for b in window_bars})
            day_payloads = []
            for d in days:
                day_window = [b for b in window_bars if b["ts_dt"].strftime("%Y-%m-%d") == d]
                first_day_ts = min(b["ts_dt"] for b in day_window)
                warm = [b for b in bars if not b["in_replay_window"] and b["ts_dt"] < first_day_ts]
                warm.sort(key=lambda x: x["ts_dt"])
                per_sym: dict[str, int] = defaultdict(int)
                kept: list[dict[str, Any]] = []
                for b in reversed(warm):
                    c = per_sym[b["symbol"]]
                    if b["symbol"] in {"SPY", "QQQ", "VIX", "^VIX"} or c < warmup_bars:
                        kept.append(b)
                        per_sym[b["symbol"]] += 1
                kept.reverse()
                shard_bars = kept + day_window
                if not shard_bars:
                    continue
                day_payloads.append({
                    "bars": shard_bars,
                    "settings": settings_dump,
                    "snapshot_db": str(snapshot_db),
                    "models_snapshot": str(models_snapshot),
                    "day": d,
                    "fill_model": fill_model,
                    "slippage_bps": slippage_bps,
                    "volume_constrained": volume_constrained,
                    "max_volume_pct": max_volume_pct,
                     "starting_cash": starting_cash,
                     "use_dynamic_scanner": use_dynamic_scanner,
                     "commission_per_share": config["commission_per_share"],
                    "minimum_commission": config["minimum_commission"],
                })

            # ``max_workers=0`` is the API-safe Windows mode. TestClient and
            # some desktop launchers invoke the async route from a worker
            # thread; spawning a ProcessPool from that context can terminate
            # the interpreter with an access violation. Run one shard at a
            # time in a dedicated thread instead. The CLI can still request
            # process parallelism explicitly.
            sequential = max_workers == 0
            workers = 1 if sequential else (max_workers or min(len(day_payloads), os.cpu_count() or 4, 10))
            trades: list[dict[str, Any]] = []
            equity: list[list[Any]] = []
            scanner_events: list[dict[str, Any]] = []
            shard_results: dict[str, dict[str, Any]] = {}
            ticks_total = 0
            all_flat = True
            done = 0
            loop = asyncio.get_running_loop()
            if sequential:
                for payload in day_payloads:
                    res = await asyncio.to_thread(run_day_shard, payload)
                    shard_results[res["day"]] = res
                    done += 1
                    ticks_total += res.get("ticks", 0)
                    all_flat = all_flat and res.get("flat_ok", True)
                    trades.extend(res.get("trades", []))
                    scanner_events.extend(
                        (res.get("scanner") or {}).get("timeline", [])
                    )
                    self.session_manager.update_session(
                        self.replay_id, progress_pct=min(99.0, 5.0 + 94.0 * done / max(1, len(day_payloads)))
                    )
            else:
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    futures = {loop.run_in_executor(pool, run_day_shard, p): p["day"] for p in day_payloads}
                    for fut in asyncio.as_completed(list(futures.keys())):
                        res = await fut
                        shard_results[res["day"]] = res
                        done += 1
                        ticks_total += res.get("ticks", 0)
                        all_flat = all_flat and res.get("flat_ok", True)
                        trades.extend(res.get("trades", []))
                        scanner_events.extend(
                            (res.get("scanner") or {}).get("timeline", [])
                        )
                        self.session_manager.update_session(
                            self.replay_id, progress_pct=min(99.0, 5.0 + 94.0 * done / max(1, len(day_payloads)))
                        )

            # Workers intentionally start from starting_cash. Rebase each day's
            # marked equity by prior realized P&L before merging; otherwise the
            # last day's equity silently replaces the account equity.
            base_starting_cash = float(starting_cash or getattr(self.settings, "paper_starting_cash", 100000.0))
            carry = base_starting_cash
            for day in sorted(shard_results):
                res = shard_results[day]
                day_trades = res.get("trades", [])
                for point in res.get("equity_curve", []):
                    equity.append([point[0], float(point[1]) + carry - base_starting_cash])
                carry += sum(float(t.get("pnl") or 0.0) for t in day_trades)
            trades.sort(key=lambda t: str(t.get("exit_at", "")))
            equity.sort(key=lambda e: e[0])
            scanner_events.sort(key=lambda event: str(event.get("timestamp", "")))
            scanner_ledger = ReplayLedger(
                f"{self.replay_id}:scanner",
                start_date=days[0] if days else start_date,
                end_date=days[-1] if days else end_date,
            )
            for event in scanner_events:
                try:
                    timestamp = datetime.fromisoformat(str(event["timestamp"]))
                    scanner_ledger.record_event(
                        str(event.get("event_type", "")),
                        timestamp,
                        str(event.get("symbol", "")),
                        dict(event.get("details") or {}),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            scanner_payload = scanner_ledger.to_payload()
            if not all_flat:
                applog.error("replay_runtime", f"Replay {self.replay_id}: at least one shard ended NON-FLAT")
            summary = {
                "replay_id": self.replay_id, "status": "COMPLETED",
                "production_ticks": ticks_total, "total_trades": len(trades),
                "days_sharded": len(day_payloads), "workers": workers,
                "all_days_flat": all_flat, "parity_verdict": "SHARDED_EXACT",
                "scanner_enabled": use_dynamic_scanner,
                "final_equity": equity[-1][1] if equity else 100000.0,
            }
            manifest = {"config_hash": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
                         "decision_path": "TradingEngine._tick", "engine": "replay_fast",
                         "model_policy": "immutable_asof_snapshot", "days": days,
                         "scanner_enabled": use_dynamic_scanner}
            self.session_manager.update_session(self.replay_id, status="COMPLETED", progress_pct=100.0,
                                                manifest=manifest, summary=summary)
            return {
                "session": summary,
                "manifest": manifest,
                "trades": trades,
                "equity_curve": equity,
                "scanner": {
                    "timeline": scanner_payload["timeline"],
                    "candidates": scanner_payload["candidates"],
                    "stage_hashes": scanner_payload["stage_hashes"],
                },
            }
        except Exception as exc:
            applog.error("replay_runtime", f"Fast replay {self.replay_id} failed: {exc}")
            self.session_manager.update_session(self.replay_id, status="FAILED",
                                                summary={"status": "FAILED", "error": str(exc)})
            raise
        finally:
            shutil.rmtree(run_snapshot, ignore_errors=True)

    async def run_replay_async(self, start_date: str, end_date: str, symbols: list[str] | None = None,
                               fill_model: str = "ohlc_touch", slippage_bps: float = 0.0, verify_parity_pass: bool = True,
                               volume_constrained: bool = False, max_volume_pct: float = 0.05, warmup_bars: int = 0) -> dict[str, Any]:
        config = {"start_date": start_date, "end_date": end_date, "symbols": symbols, "fill_model": fill_model, "slippage_bps": slippage_bps,
                  "volume_constrained": volume_constrained, "max_volume_pct": max_volume_pct, "warmup_bars": warmup_bars}
        self.session_manager.create_session(self.replay_id, start_date, end_date, config)
        self.session_manager.update_session(self.replay_id, status="RUNNING", progress_pct=2.0)
        parity: dict[str, Any] | None = None
        run_snapshot = Path(tempfile.mkdtemp(prefix="alpha_replay_run_snapshot_"))
        try:
            # Scanner Parity: Include dynamically traded symbols from live db
            if symbols is not None:
                target_symbols = symbols
            else:
                base_watchlist = self.source_db.get_setting("watchlist") or self.settings.watchlist_symbols()
                live_symbols = set()
                try:
                    # FIX(#F15): Database exposes .engine, not .conn -- the old
                    # AttributeError was swallowed and live-parity symbols were
                    # always silently empty.
                    with self.source_db.engine.connect() as conn:
                        rows = conn.exec_driver_sql("SELECT symbol, payload FROM trades").fetchall()
                    import json as _json
                    for sym, payload in rows:
                        if not payload:
                            continue
                        try:
                            p = _json.loads(payload)
                            entry_at = str(p.get("entry_at", ""))
                            if start_date <= entry_at <= end_date + "T23:59:59":
                                live_symbols.add(sym)
                        except Exception:
                            continue
                except Exception:
                    pass
                target_symbols = list(set(base_watchlist) | live_symbols)

            from datetime import timedelta
            history_start = start_date if self.feed._explicit_data_path else (datetime.fromisoformat(start_date).date() - timedelta(days=60)).isoformat()
            await self.feed.ensure_data(history_start, end_date, target_symbols)

            bars = self.feed.load_bars(history_start, end_date, target_symbols)
            for bar in bars:
                bar["in_replay_window"] = start_date <= bar["ts_dt"].strftime("%Y-%m-%d") <= end_date

            # Filter bars to include only requested warmup + replay window
            filtered_bars = []
            from collections import defaultdict
            by_sym = defaultdict(list)
            for b in bars:
                by_sym[b["symbol"]].append(b)

            for sym, sym_bars in by_sym.items():
                sym_bars.sort(key=lambda x: x["ts_dt"])
                first_in_window = next((i for i, b in enumerate(sym_bars) if b["in_replay_window"]), len(sym_bars))
                start_idx = 0 if sym in {"SPY", "QQQ", "VIX", "^VIX"} else max(0, first_in_window - warmup_bars)
                filtered_bars.extend(sym_bars[start_idx:])

            bars = sorted(filtered_bars, key=lambda b: (b["ts_dt"], b["symbol"]))

            replay_bars = [b for b in bars if b.get("in_replay_window", True)]
            if not replay_bars:
                raise RuntimeError("No bars in requested replay window")
            model_asof = replay_bars[-1]["ts_dt"]
            snapshot_db = run_snapshot / "source.db"
            self._snapshot_source_database(snapshot_db)
            snapshot_models = run_snapshot / "models"

            # Model Versioning: Attempt to load from archive if model_asof is provided
            archived_loaded = False
            if model_asof:
                asof_dt = model_asof
                if asof_dt.tzinfo is None:
                    asof_dt = asof_dt.replace(tzinfo=UTC)
                from zoneinfo import ZoneInfo
                ET = ZoneInfo("America/New_York")
                asof_date_str = asof_dt.astimezone(ET).strftime("%Y-%m-%d")
                archive_dir = MODELS_DIR / "archive" / asof_date_str
                if archive_dir.exists():
                    # Copy only decision-relevant subtrees; historical archives
                    # inside the archive dir would balloon the snapshot (300MB+).
                    import shutil as _sh
                    snapshot_models.mkdir(parents=True, exist_ok=True)
                    for item in archive_dir.iterdir():
                        dst = snapshot_models / item.name
                        if item.is_dir():
                            _sh.copytree(item, dst, dirs_exist_ok=True)
                        else:
                            _sh.copy2(item, dst)
                    archived_loaded = True

            if not archived_loaded:
                # Perf: exclude 'archive' and 'generations' -- not part of the
                # live inference surface and hundreds of MB.
                import shutil as _sh
                IGNORE = shutil.ignore_patterns("archive", "generations", ".specialist-candidate-*")
                _sh.copytree(MODELS_DIR, snapshot_models, ignore=IGNORE, dirs_exist_ok=True)

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
            a = await self._execute_single_pass(bars, fill_model, slippage_bps, "A", snapshot_db, snapshot_models, volume_constrained, max_volume_pct, warmup_bars)
            self.session_manager.update_session(self.replay_id, progress_pct=100.0)
            self._assert_meaningful_execution(a)

            summary = {"replay_id": self.replay_id, "status": "COMPLETED", "production_ticks": a.production_ticks,
                       "total_candidates": len(a.candidates), "total_signals": len(a.signals), "total_trades": len(a.trades),
                       "total_orders": len(a.orders), "total_fills": len(a.fills), "final_equity": a.equity_curve[-1]["equity"] if a.equity_curve else 100000.0,
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
                                   pass_name: str, snapshot_db: Path, snapshot_models: Path,
                                   volume_constrained: bool = False, max_volume_pct: float = 0.05, warmup_bars: int = 0) -> ReplayLedger:
        replay_bars = [b for b in bars if b.get("in_replay_window", True)]
        if not replay_bars:
            raise RuntimeError("No bars in requested replay window")
        first_ts = replay_bars[0]["ts_dt"]
        clock, data = ReplayClock(first_ts, always_open=False), ReplayDataManager(bars)
        broker = ReplayBroker(float(getattr(self.settings, "paper_starting_cash", 100000.0)), slippage_bps, fill_model,
                              volume_constrained=volume_constrained, max_volume_pct=max_volume_pct)
        start_date_str = replay_bars[0]["ts_dt"].strftime("%Y-%m-%d")
        end_date_str = replay_bars[-1]["ts_dt"].strftime("%Y-%m-%d")
        ledger = ReplayLedger(f"{self.replay_id}:{pass_name}", start_date=start_date_str, end_date=end_date_str)
        broker.attach_ledger(ledger)
        tmp_path = Path(tempfile.mkdtemp(prefix="alpha_replay_"))
        replay_db: Database | None = None
        engine: TradingEngine | None = None
        # Rejections observed inside a replay are synthetic decisions; writing
        # them into the production shadow ledger polluted forensics and stalled
        # the resolver on rows whose forward bars only exist inside the replay.
        # Suppress only this replay context so the live resolver/engine remain
        # able to write and resolve production forensics concurrently.
        from app.core.shadow_ledger import shadow_ledger as _prod_shadow
        _shadow_scope = _prod_shadow.disabled()
        _shadow_scope.__enter__()
        try:
            replay_db_path = tmp_path / "replay.db"
            shutil.copy2(snapshot_db, replay_db_path)
            replay_db = Database(replay_db_path)
            # Perf: throwaway DB -- disable durability fsyncs entirely. The
            # authoritative run artifacts are the ledger/timeline, not this db.
            with replay_db.engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=MEMORY")
                conn.exec_driver_sql("PRAGMA synchronous=OFF")
                conn.exec_driver_sql("PRAGMA temp_store=MEMORY")
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
            window_timestamps = {bar["ts_dt"] for bar in bars if bar.get("in_replay_window", False)}
            min_window_ts = min(window_timestamps) if window_timestamps else min(b["ts_dt"] for b in bars)
            pre_window_ts = sorted({b["ts_dt"] for b in bars if b["ts_dt"] < min_window_ts})
            if int(warmup_bars) <= 0:
                tick_start_ts = min_window_ts
            elif len(pre_window_ts) >= int(warmup_bars):
                tick_start_ts = pre_window_ts[-int(warmup_bars)]
            else:
                tick_start_ts = pre_window_ts[0] if pre_window_ts else min_window_ts
            for bar in bars:
                if bar["ts_dt"] >= tick_start_ts:
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
            with ExitStack() as stack:
                stack.enter_context(patch("app.core.engine.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.core.scanner.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.risk.manager.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.risk.market_regime.datetime", ContextAwareDateTime))
                stack.enter_context(patch("app.core.domain.uuid", deterministic_uuid))
                for idx, ts in enumerate(sorted(by_ts)):
                    if self.session_manager.is_cancelled(self.replay_id): raise RuntimeError("Replay cancelled")
                    clock.set_time(ts); data.set_time(ts)
                    ts_bars = by_ts[ts]
                    is_replay_window = any(b.get("in_replay_window", True) for b in ts_bars)
                    for bar in sorted(ts_bars, key=lambda b: b["symbol"]):
                        close = float(bar.get("close", bar.get("c")))
                        vol = float(bar.get("volume", bar.get("v", 0.0)))
                        broker.process_ohlc(bar["symbol"], float(bar.get("open", bar.get("o", close))),
                                            float(bar.get("high", bar.get("h", close))),
                                            float(bar.get("low", bar.get("l", close))), close, ts, volume=vol)
                    engine.allow_new_entries = is_replay_window
                    # _force_gated survives _check_system_heartbeat's recompute
                    # of allow_new_entries; keeps warmup history entry-free.
                    engine._force_gated = not is_replay_window
                    await engine._tick()
                    if is_replay_window:
                        ledger.production_ticks += 1
                        acct, positions = await broker.account(), await broker.positions()
                        ledger.record_equity(ts, acct.equity, acct.cash, acct.buying_power, len(positions))
                    if idx % 100 == 0:
                        self.session_manager.update_session(self.replay_id, progress_pct=min(49.0, 5.0 + 44.0 * idx / max(1, len(by_ts))))
            await self._stop_replay_engine(engine)
            self._hydrate_db_ledger(replay_db, ledger)
            return ledger
        finally:
            _shadow_scope.__exit__(None, None, None)
            import app.db.database as db_module
            db_module._ACTIVE_DATABASE = self.source_db
            if engine is not None: await self._stop_replay_engine(engine)
            if replay_db is not None: replay_db.engine.dispose(close=True)
            shutil.rmtree(tmp_path, ignore_errors=False)

    @staticmethod
    async def _stop_replay_engine(engine: TradingEngine) -> None:
        await asyncio.sleep(0)

    async def _initialize_engine(self, engine: TradingEngine, clock: ReplayClock, broker: ReplayBroker, data: ReplayDataManager, ledger: ReplayLedger, replay_models: Path) -> None:
        from app.portfolio.allocator import CapitalAllocationOptimizer, PortfolioAIGate
        from app.portfolio.tracker import PortfolioTracker
        from app.risk.manager import RiskManager
        from app.strategies import SignalPipeline
        engine.mode = "paper"; engine.clock, engine.data, engine.broker, engine.runtime_ledger = clock, data, broker, ledger
        # Perf: PIS excursion memory is per-run state; skip per-bar persistence.
        if hasattr(engine, "pis_adapter"):
            engine.pis_adapter.persist_profit_memory = False
        engine.risk = RiskManager(engine.risk_config, clock); engine.risk.core_watchlist = engine.core_watchlist
        engine.portfolio = PortfolioTracker()
        engine.portfolio_gate = PortfolioAIGate(max_correlation=engine.risk_config.max_correlation_penalty, max_positions_per_sector=engine.risk_config.max_positions_per_sector)
        engine.portfolio_allocator = CapitalAllocationOptimizer(kelly_fraction_cap=engine.risk_config.kelly_fraction_cap)
        active = engine.db.get_setting("active_policy_snapshot")
        ml = float(active["ml_threshold"]) if active and "ml_threshold" in active else float(engine.db.get_setting("ml_threshold") or engine.risk_config.ml_confidence_threshold)
        engine.risk_config.ml_confidence_threshold = ml
        engine.pipeline = SignalPipeline(engine.model, ml, risk_config=engine.risk_config, settings=engine.settings)

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

        account = await broker.account(); engine.portfolio.roll_session(clock.session_date(clock.now_utc()), account.equity)
        engine.portfolio.peak_equity = account.equity; engine.portfolio.mark_equity(account.equity, clock.now_utc())
        engine.state = EngineState.RUNNING; engine.publish = ledger.capture_publish

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
        cutoff_utc = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
        cutoff_epoch = cutoff_utc.timestamp()
        kept: list[str] = []
        excluded: list[dict[str, str]] = []
        for path in sorted(p for p in models_path.rglob("*") if p.is_file()):
            rel = str(path.relative_to(models_path)).replace("\\", "/")
            mtime = path.stat().st_mtime
            if mtime > cutoff_epoch:
                # No tier exemptions: letting post-cutoff meta/specialist artifacts
                # into a historical replay is temporal leakage in exactly the tool
                # meant to prove fidelity.
                excluded.append({"path": rel, "available_at": datetime.fromtimestamp(mtime, UTC).isoformat()})
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
        pass

    @staticmethod
    def _clear_mutable_replay_tables(db: Database) -> None:
        with db.engine.begin() as conn:
            for table in ("orders","trades","equity_snapshots"):
                try:
                    conn.exec_driver_sql(f"DELETE FROM {table}")
                except Exception:
                    pass
            conn.exec_driver_sql("DELETE FROM app_settings WHERE key IN ('pending_reconciliation_orders','heartbeat_write_check')")

    @staticmethod
    def _hydrate_db_ledger(db: Database, ledger: ReplayLedger) -> None:
        start_date = getattr(ledger, "start_date", None)
        with db.engine.connect() as conn:
            for row in conn.exec_driver_sql("SELECT exit_at,symbol,payload FROM trades ORDER BY exit_at,id").fetchall():
                try: details = json.loads(row[2])
                except Exception: continue
                ts = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]))
                if start_date and ts.strftime("%Y-%m-%d") < start_date: continue
                ledger.record_event("TRADE_CLOSED", ts, row[1], details)

    def _build_manifest(self, config: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
        digest = hashlib.sha256("\n".join(json.dumps({k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in b.items() if k!="ts_dt"},sort_keys=True,default=str) for b in bars).encode()).hexdigest()
        try: commit = subprocess.check_output(["git","rev-parse","HEAD"],text=True,stderr=subprocess.DEVNULL).strip()
        except Exception: commit = os.getenv("GIT_COMMIT_SHA","unknown")
        window=[b for b in bars if b.get("in_replay_window",True)]; syms=sorted({b["symbol"] for b in bars})
        return {"replay_id":self.replay_id,"engine_commit":commit,"config_hash":hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),"data_hash":digest,"bar_count_total":len(bars),"bar_count_replay_window":len(window),"symbols":syms,"first_timestamp":bars[0]["ts_dt"].isoformat(),"last_timestamp":bars[-1]["ts_dt"].isoformat(),"decision_path":"TradingEngine._tick","lookahead_policy":"strict_as_of","database_policy":"single_immutable_run_snapshot_cloned_per_pass","model_policy":"immutable_asof_snapshot_cloned_per_pass","clock_policy":"ReplayClock + engine/scanner/risk datetime binding","identity_policy":"deterministic replay-only UUID binding","context_memory_policy":"immutable_run_start_snapshot_no_replay_feedback","market_risk_policy":"historical SPY+QQQ+VIX required; no UNKNOWN substitution","strategy_observation_policy":"replay-only wrapper around production on_bar","risk_observation_policy":"replay-only wrapper around production evaluate_entry","fill_model":config["fill_model"],"slippage_bps":config["slippage_bps"],"model_artifacts":self._model_artifact_manifest()}

    @staticmethod
    def _model_artifact_manifest() -> dict[str, Any]:
        from app.config import MODELS_DIR
        import hashlib
        result: dict[str, Any] = {"global": {}, "specialists": {}}
        for path in sorted(MODELS_DIR.glob("signal_model*.joblib")):
            result["global"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        meta = MODELS_DIR / "signal_model.meta.json"
        if meta.exists():
            result["global"][meta.name] = hashlib.sha256(meta.read_bytes()).hexdigest()
        for path in sorted((MODELS_DIR / "specialists").glob("*.pkl")):
            result["specialists"][path.stem.upper()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    @staticmethod
    def _assert_meaningful_execution(ledger: ReplayLedger) -> None:
        if ledger.production_ticks <= 0: raise RuntimeError("Replay executed zero production ticks")
        if not ledger.equity_curve: raise RuntimeError("Replay produced no portfolio states")
