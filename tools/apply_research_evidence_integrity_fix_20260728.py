from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/app/db/database.py"
ENGINE = ROOT / "backend/app/core/engine.py"
TARGET_D = ROOT / "backend/app/ai/target_d_observer.py"
RUNTIME = ROOT / "backend/app/research/shadow_runtime.py"
SPAWN = ROOT / "backend/app/research/spawn/spawn_executor.py"
COORD = ROOT / "backend/app/research/spawn/spawn_shadow_coordinator.py"
PUBLISHER = ROOT / "backend/app/research/shadow_event_publisher.py"
TEST = ROOT / "backend/tests/test_research_evidence_integrity_20260728.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"anchor not found for {label}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# 1. SQLite: transient research-writer contention must not become a prod halt.
# ---------------------------------------------------------------------------
db = DB.read_text(encoding="utf-8")
db = replace_once(
    db,
    '        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})\n        Base.metadata.create_all(self.engine)',
    '''        # Production and passive research share this SQLite file.  Give the single\n        # writer time to clear transient contention and let readers coexist via WAL.\n        self.engine = create_engine(\n            f"sqlite:///{path}",\n            connect_args={"check_same_thread": False, "timeout": 30.0},\n        )\n        with self.engine.connect() as conn:\n            conn.exec_driver_sql("PRAGMA busy_timeout=30000")\n            conn.exec_driver_sql("PRAGMA journal_mode=WAL")\n            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")\n        Base.metadata.create_all(self.engine)''',
    "sqlite contention hardening",
)
db = replace_once(
    db,
    '\n\nclass ContextualPerformanceMemoryRow(Base):',
    '''\n\nclass HarvestShadowStateRow(Base):\n    """Durable research-only Harvest-V2 counterfactual state."""\n    __tablename__ = "harvest_shadow_states"\n    id: Mapped[str] = mapped_column(String(160), primary_key=True)\n    ts: Mapped[datetime]\n    symbol: Mapped[str] = mapped_column(String(12))\n    mode: Mapped[str] = mapped_column(String(10))\n    payload: Mapped[str] = mapped_column(Text)\n\n\nclass TargetDObservationRow(Base):\n    """Durable research-only Target-D observation, including UNAVAILABLE states."""\n    __tablename__ = "target_d_observations"\n    id: Mapped[str] = mapped_column(String(160), primary_key=True)\n    ts: Mapped[datetime]\n    candidate_id: Mapped[str | None] = mapped_column(String(128), nullable=True)\n    symbol: Mapped[str] = mapped_column(String(12))\n    strategy: Mapped[str] = mapped_column(String(32))\n    decision: Mapped[str] = mapped_column(String(20))\n    payload: Mapped[str] = mapped_column(Text)\n\n\nclass ContextualPerformanceMemoryRow(Base):''',
    "research evidence tables",
)
db = replace_once(
    db,
    '    # ---- durable broker fill / portfolio checkpoint contract ----',
    '''    # ---- passive research evidence ----\n\n    def save_harvest_shadow_state(\n        self, observation_id: str, ts: datetime, symbol: str, mode: str, payload: dict\n    ) -> None:\n        with self._session() as s:\n            s.merge(HarvestShadowStateRow(\n                id=observation_id, ts=ts, symbol=symbol.upper(), mode=mode,\n                payload=json.dumps(payload, default=str, sort_keys=True),\n            ))\n            s.commit()\n\n    def get_harvest_shadow_states(self, mode: str | None = None) -> list[dict]:\n        with self._session() as s:\n            stmt = select(HarvestShadowStateRow).order_by(HarvestShadowStateRow.ts.asc())\n            if mode:\n                stmt = stmt.where(HarvestShadowStateRow.mode == mode)\n            return [json.loads(row.payload) for row in s.scalars(stmt)]\n\n    def save_target_d_observation(self, observation: dict) -> None:\n        raw_ts = observation.get("ts") or observation.get("evaluated_at")\n        if isinstance(raw_ts, datetime):\n            ts = raw_ts\n        elif raw_ts:\n            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))\n        else:\n            ts = datetime.now(UTC)\n        event_id = str(observation.get("event_id") or observation.get("candidate_id") or f"target_d:{ts.isoformat()}")\n        with self._session() as s:\n            s.merge(TargetDObservationRow(\n                id=event_id, ts=ts, candidate_id=observation.get("candidate_id"),\n                symbol=str(observation.get("symbol") or "")[:12].upper(),\n                strategy=str(observation.get("strategy") or "unknown")[:32],\n                decision=str(observation.get("target_d_decision") or "UNAVAILABLE")[:20],\n                payload=json.dumps(observation, default=str, sort_keys=True),\n            ))\n            s.commit()\n\n    def get_target_d_observations(self, limit: int = 5000) -> list[dict]:\n        with self._session() as s:\n            stmt = select(TargetDObservationRow).order_by(TargetDObservationRow.ts.desc()).limit(limit)\n            return [json.loads(row.payload) for row in s.scalars(stmt)]\n\n    # ---- durable broker fill / portfolio checkpoint contract ----''',
    "research persistence methods",
)
DB.write_text(db, encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. Engine mirror: every genuine strategy candidate must reach existing shadows.
#    Pipeline rejects are mirrored here; pipeline passes are mirrored later after
#    portfolio allocation, so candidate IDs are still published exactly once.
#    Feed Target-D the SAME feature row used by the existing ML feature pipeline.
# ---------------------------------------------------------------------------
engine = ENGINE.read_text(encoding="utf-8")
engine = replace_once(
    engine,
    'from app.ai.insights import InsightEngine\n',
    'from app.ai.insights import InsightEngine\nfrom app.ai.features import latest_feature_row\n',
    "canonical feature import",
)
engine = replace_once(
    engine,
    '''            # Run and record all active Challengers (CR-146 Phase 3)''',
    '''            # Pipeline-rejected candidates never reach portfolio allocation. Mirror\n            # them here so existing Spawn/Target-D/outcome consumers see the complete\n            # opportunity set. Pipeline-approved candidates are mirrored later with the\n            # final portfolio decision, preserving one canonical event per candidate.\n            if not is_accepted:\n                try:\n                    feature_row = latest_feature_row(df)\n                    gate_ev = parallel_evidences.get(sig.candidate_id) or {}\n                    latest_row = df.iloc[-1] if not df.empty else None\n                    rejected_event = candidate_event_from_signal(\n                        sig,\n                        final_decision="rejected",\n                        rejection_reason=rejection_reason,\n                        features=feature_row.to_dict() if feature_row is not None else None,\n                        open_=float(latest_row["open"]) if latest_row is not None and "open" in latest_row and pd.notna(latest_row["open"]) else None,\n                        high=float(latest_row["high"]) if latest_row is not None and "high" in latest_row and pd.notna(latest_row["high"]) else None,\n                        low=float(latest_row["low"]) if latest_row is not None and "low" in latest_row and pd.notna(latest_row["low"]) else None,\n                        close=float(latest_row["close"]) if latest_row is not None and "close" in latest_row and pd.notna(latest_row["close"]) else None,\n                        atr=float(latest_row["atr14"]) if latest_row is not None and "atr14" in latest_row and pd.notna(latest_row["atr14"]) else None,\n                        rsi=float(latest_row["rsi2"]) if latest_row is not None and "rsi2" in latest_row and pd.notna(latest_row["rsi2"]) else None,\n                        meta_probability=gate_ev.get("meta_probability"),\n                        meta_decision=("PASS" if gate_ev.get("meta_passed") is True else "REJECT" if gate_ev.get("meta_passed") is False else None),\n                        trade_score_decision=("PASS" if gate_ev.get("trade_score_passed") is True else "REJECT" if gate_ev.get("trade_score_passed") is False else None),\n                        vix=self._last_vix_value,\n                        vix_status=self._last_vix_status,\n                    )\n                    self.shadow_research.publish(rejected_event)\n                except Exception as exc:\n                    applog.debug("shadow_research", f"pipeline-reject mirror failed open for {sig.symbol}: {exc}")\n\n            # Run and record all active Challengers (CR-146 Phase 3)''',
    "pipeline reject canonical mirror",
)
engine = replace_once(
    engine,
    '''                        event = candidate_event_from_signal(\n                            sig,\n                            final_decision="accepted" if approved else "rejected",\n                            rejection_reason=None if approved else gated_reasons.get(cid, "portfolio/allocation rejected"),''',
    '''                        feature_row = latest_feature_row(df)\n                        gate_ev = parallel_evidences.get(cid) or {}\n                        event = candidate_event_from_signal(\n                            sig,\n                            final_decision="accepted" if approved else "rejected",\n                            rejection_reason=None if approved else gated_reasons.get(cid, "portfolio/allocation rejected"),\n                            features=feature_row.to_dict() if feature_row is not None else None,''',
    "accepted candidate feature mirror",
)
engine = replace_once(
    engine,
    '''                            meta_probability=getattr(sig, "meta_probability", None),\n                            portfolio_decision="PASS" if approved else "REJECT",''',
    '''                            meta_probability=gate_ev.get("meta_probability"),\n                            meta_decision=("PASS" if gate_ev.get("meta_passed") is True else "REJECT" if gate_ev.get("meta_passed") is False else None),\n                            trade_score_decision=("PASS" if gate_ev.get("trade_score_passed") is True else "REJECT" if gate_ev.get("trade_score_passed") is False else None),\n                            portfolio_decision="PASS" if approved else "REJECT",''',
    "accepted gate evidence mirror",
)
engine = replace_once(
    engine,
    '''                st["winner_capture"] = (st["shadow_realized_r"] / st["mfe_r"]) if st["shadow_realized_r"] > 0 and st["mfe_r"] > 0 else 0.0\n\n        except Exception as exc:''',
    '''                st["winner_capture"] = (st["shadow_realized_r"] / st["mfe_r"]) if st["shadow_realized_r"] > 0 and st["mfe_r"] > 0 else 0.0\n\n            # Passive research durability only; never feed this result back into Champion.\n            try:\n                observation_id = f"{self.mode}:{symbol}:{st['observation_started_at']}"\n                persist_ts = current_bar_ts.to_pydatetime() if hasattr(current_bar_ts, "to_pydatetime") else current_bar_ts\n                self.db.save_harvest_shadow_state(observation_id, persist_ts, symbol, self.mode, dict(st))\n            except Exception as persist_exc:\n                applog.warning("engine.v2_shadow", f"V2 shadow evidence persistence failed for {symbol}: {persist_exc}")\n\n        except Exception as exc:''',
    "harvest engine persistence",
)
ENGINE.write_text(engine, encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. Target-D: explicit artifact health + durable independent observations.
# ---------------------------------------------------------------------------
target = TARGET_D.read_text(encoding="utf-8")
target = replace_once(target, '''        self.threshold = 0.48\n        self._load_artifact()''', '''        self.threshold = 0.48\n        self.artifact_status = "UNINITIALIZED"\n        self.artifact_error = None\n        self._load_artifact()''', "target d status fields")
target = replace_once(target, '''        try:\n            if os.path.exists(self.model_path):\n                self.model = joblib.load(self.model_path)\n            if os.path.exists(self.meta_path):''', '''        try:\n            if not os.path.exists(self.model_path):\n                self.artifact_status = "MISSING_MODEL"\n                self.artifact_error = self.model_path\n                applog.warning("target_d_observer", f"Target D model unavailable: {self.model_path}")\n                return\n            self.model = joblib.load(self.model_path)\n            if os.path.exists(self.meta_path):''', "target d missing model diagnostics")
target = replace_once(target, '''                self.threshold = float(self.metadata.get("threshold", 0.48))\n        except Exception as exc:\n            applog.error("target_d_observer", f"Failed to load Target D artifact: {exc}")\n            self.model = None''', '''                self.threshold = float(self.metadata.get("threshold", 0.48))\n            self.artifact_status = "READY"\n            self.artifact_error = None\n        except Exception as exc:\n            self.artifact_status = "LOAD_ERROR"\n            self.artifact_error = f"{type(exc).__name__}: {exc}"\n            applog.error("target_d_observer", f"Failed to load Target D artifact: {exc}")\n            self.model = None''', "target d load diagnostics")
target = replace_once(target, '''            "generation_id": self.generation_id,\n            "disagreement_category": "UNAVAILABLE"\n        }''', '''            "generation_id": self.generation_id,\n            "disagreement_category": "UNAVAILABLE",\n            "artifact_status": self.artifact_status,\n            "unavailable_reason": self.artifact_error or ("FEATURES_UNAVAILABLE" if features is None else "MODEL_UNAVAILABLE"),\n        }''', "target d unavailable reason")
TARGET_D.write_text(target, encoding="utf-8")

runtime = RUNTIME.read_text(encoding="utf-8")
runtime = replace_once(runtime, '''        def target_d_sink(observation: dict[str, Any]) -> None:\n            if target_d_observation_sink is not None:\n                target_d_observation_sink(observation)''', '''        def target_d_sink(observation: dict[str, Any]) -> None:\n            try:\n                db.save_target_d_observation(observation)\n            except Exception as exc:\n                applog.warning("target_d_shadow", f"Target D durable sink failed open: {exc}")\n            if target_d_observation_sink is not None:\n                target_d_observation_sink(observation)''', "target d durable sink")
runtime = replace_once(runtime, '''                "exit_challengers": self.exits.telemetry(), "started": self._started,\n                "lifecycle_errors": self._lifecycle_errors}''', '''                "exit_challengers": self.exits.telemetry(),\n                "target_d": {"artifact_status": self.target_d.observer.artifact_status,\n                             "artifact_error": self.target_d.observer.artifact_error},\n                "started": self._started, "lifecycle_errors": self._lifecycle_errors}''', "target d runtime telemetry")
RUNTIME.write_text(runtime, encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. Spawn: never fabricate missing meta evidence or a synthetic 2% stop.
#    Those made historical child decisions look more complete than the evidence was.
# ---------------------------------------------------------------------------
spawn = SPAWN.read_text(encoding="utf-8")
spawn = replace_once(
    spawn,
    '''        else:\n            categorical_pipeline_pass=champion.get("final_decision") in("accepted","rejected","bypassed"); meta_pass=categorical_pipeline_pass; meta_decision="PASS_PRODUCTION_PIPELINE_EVIDENCE" if meta_pass else "UNAVAILABLE"''',
    '''        else:\n            # Missing meta probability is missing evidence.  A Champion rejection does\n            # not prove the meta gate passed, and must never be converted into a pass.\n            meta_pass=False; meta_decision="UNAVAILABLE"''',
    "spawn missing meta evidence",
)
spawn = replace_once(
    spawn,
    '''        stop=self._finite(observation.get("stop_loss")); target=self._finite(observation.get("take_profit"))\n        if stop is None:\n            distance=price*0.02; stop=price-distance if direction=="long" else price+distance\n        qty=self.portfolio.calculate_position_size(price,stop)''',
    '''        stop=self._finite(observation.get("stop_loss")); target=self._finite(observation.get("take_profit"))\n        # Missing/wrong-side risk is unavailable evidence, not permission to invent a\n        # 2% stop. Keep the evaluation record but prevent a fabricated virtual trade.\n        valid_stop = stop is not None and ((direction == "long" and stop < price) or (direction == "short" and stop > price))\n        qty=self.portfolio.calculate_position_size(price,stop) if valid_stop else 0.0''',
    "spawn synthetic stop removal",
)
spawn = replace_once(
    spawn,
    '''        risk_decision="PASS" if capital_pass else f"REJECT_CAPITAL_{allocation_reason}"\n        hypothetical_pass=signal_generated and ml_pass and meta_pass and score_pass and capital_pass''',
    '''        risk_decision=("UNAVAILABLE_MISSING_OR_INVALID_STOP" if not valid_stop else "PASS" if capital_pass else f"REJECT_CAPITAL_{allocation_reason}")\n        hypothetical_pass=signal_generated and ml_pass and meta_pass and score_pass and capital_pass and valid_stop''',
    "spawn invalid risk rejection",
)
SPAWN.write_text(spawn, encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. Spawn observability: distinguish an alive worker with no open positions from
#    a stopped worker. No broker/data authority is added.
# ---------------------------------------------------------------------------
coord = COORD.read_text(encoding="utf-8")
coord = replace_once(coord, '''        self.evolution_cycles = 0\n''', '''        self.evolution_cycles = 0\n        self.last_observation_ts = None\n        self.last_candidate_ts = None\n        self.last_market_bar_ts = None\n''', "spawn liveness fields")
coord = replace_once(coord, '''            "generation": self.evolution.generation,\n        }''', '''            "generation": self.evolution.generation,\n            "last_observation_ts": self.last_observation_ts.isoformat() if self.last_observation_ts else None,\n            "last_candidate_ts": self.last_candidate_ts.isoformat() if self.last_candidate_ts else None,\n            "last_market_bar_ts": self.last_market_bar_ts.isoformat() if self.last_market_bar_ts else None,\n        }''', "spawn liveness telemetry")
coord = replace_once(coord, '''        self.observations_processed += 1\n        if is_market_bar:\n            self.market_bars_processed += 1\n            return\n\n        self.candidate_observations_processed += 1''', '''        self.observations_processed += 1\n        obs_ts = observation.get("ts")\n        if isinstance(obs_ts, datetime):\n            self.last_observation_ts = obs_ts\n        if is_market_bar:\n            self.market_bars_processed += 1\n            if isinstance(obs_ts, datetime):\n                self.last_market_bar_ts = obs_ts\n            return\n\n        self.candidate_observations_processed += 1\n        if isinstance(obs_ts, datetime):\n            self.last_candidate_ts = obs_ts''', "spawn liveness updates")
COORD.write_text(coord, encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. Publisher observability + graceful drain. Research remains bounded/fail-open.
# ---------------------------------------------------------------------------
publisher = PUBLISHER.read_text(encoding="utf-8")
publisher = replace_once(publisher, '''        self.consumer_errors = 0\n        self.last_processed_at: datetime | None = None''', '''        self.consumer_errors = 0\n        self.consumer_errors_by_name: dict[str, int] = {}\n        self.last_processed_at: datetime | None = None''', "publisher per-consumer errors")
publisher = replace_once(publisher, '''    async def stop(self) -> None:\n        self.running = False\n        if self._worker_task is not None:\n            self._worker_task.cancel()''', '''    async def stop(self) -> None:\n        # Give already-accepted research observations a bounded chance to drain.\n        # Shutdown must still complete even if a research consumer is unhealthy.\n        if self._worker_task is not None and self.running:\n            try:\n                await asyncio.wait_for(self.queue.join(), timeout=5.0)\n            except (TimeoutError, asyncio.TimeoutError):\n                applog.warning("shadow_event_publisher", f"Shutdown drain timed out with {self.queue.qsize()} events pending")\n        self.running = False\n        if self._worker_task is not None:\n            self._worker_task.cancel()''', "publisher graceful drain")
publisher = replace_once(publisher, '''                        except Exception as exc:\n                            self.consumer_errors += 1\n                            applog.warning("shadow_event_publisher", f"Consumer {registration.name} failed open: {exc}")''', '''                        except Exception as exc:\n                            self.consumer_errors += 1\n                            self.consumer_errors_by_name[registration.name] = self.consumer_errors_by_name.get(registration.name, 0) + 1\n                            applog.warning("shadow_event_publisher", f"Consumer {registration.name} failed open: {exc}")''', "publisher consumer error attribution")
publisher = replace_once(publisher, '''            "events_duplicate": self.events_duplicate, "consumer_errors": self.consumer_errors,\n            "consumer_count": len(self._consumers), "lag_seconds": lag_seconds,''', '''            "events_duplicate": self.events_duplicate, "consumer_errors": self.consumer_errors,\n            "consumer_errors_by_name": dict(self.consumer_errors_by_name),\n            "consumer_count": len(self._consumers), "lag_seconds": lag_seconds,''', "publisher error telemetry")
PUBLISHER.write_text(publisher, encoding="utf-8")


TEST.write_text('''from __future__ import annotations\n\nfrom datetime import UTC, datetime\nfrom app.ai.target_d_observer import TargetDShadowObserver\nfrom app.db.database import Database\nfrom app.research.spawn.spawn_config import ChildPolicyConfig\nfrom app.research.spawn.spawn_executor import ChildPolicyExecutor\n\n\ndef test_sqlite_runtime_uses_wal_and_busy_timeout(tmp_path):\n    db = Database(tmp_path / "research-integrity.db")\n    with db.engine.connect() as conn:\n        assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal"\n        assert int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar()) >= 30000\n\n\ndef test_harvest_shadow_state_is_durable_and_upserts(tmp_path):\n    path = tmp_path / "research-integrity.db"; db = Database(path)\n    ts1 = datetime(2026, 7, 28, 13, 31, tzinfo=UTC); ts2 = datetime(2026, 7, 28, 13, 32, tzinfo=UTC)\n    oid = "paper:NVDA:2026-07-28T13:30:00+00:00"\n    db.save_harvest_shadow_state(oid, ts1, "NVDA", "paper", {"harvest_state": "runner_preserve", "mfe_r": 0.5})\n    db.save_harvest_shadow_state(oid, ts2, "NVDA", "paper", {"harvest_state": "confirmed_giveback", "mfe_r": 1.8, "shadow_realized_r": 1.2})\n    rows = Database(path).get_harvest_shadow_states("paper")\n    assert len(rows) == 1 and rows[0]["harvest_state"] == "confirmed_giveback" and rows[0]["mfe_r"] == 1.8\n\n\ndef test_target_d_unavailable_is_explicit(tmp_path):\n    observer = TargetDShadowObserver(str(tmp_path / "missing.joblib"), str(tmp_path / "missing.json"))\n    result = observer.evaluate_passive({}, 0.7, "PASS")\n    assert result["target_d_decision"] == "UNAVAILABLE"\n    assert result["artifact_status"] == "MISSING_MODEL"\n    assert result["unavailable_reason"]\n\n\ndef test_target_d_observation_is_durable(tmp_path):\n    path = tmp_path / "research-integrity.db"; db = Database(path)\n    obs = {"event_id": "evt-1", "candidate_id": "cand-1", "symbol": "NVDA", "strategy": "momentum", "ts": "2026-07-28T13:31:00+00:00", "target_d_decision": "UNAVAILABLE", "artifact_status": "MISSING_MODEL"}\n    db.save_target_d_observation(obs)\n    rows = Database(path).get_target_d_observations()\n    assert len(rows) == 1 and rows[0]["candidate_id"] == "cand-1"\n\n\ndef _spawn_config() -> ChildPolicyConfig:\n    return ChildPolicyConfig(\n        policy_id="test-shadow-integrity", spawned_at=datetime(2026, 7, 28, 13, 30, tzinfo=UTC),\n        seed_capital=2500.0, execution_mode="SHADOW_ONLY",\n        ml_overrides={"ml_threshold": 0.5}, meta_overrides={"meta_threshold": 0.55},\n        risk_overrides={"min_trade_score": 50.0, "max_position_pct": 25.0, "risk_per_trade_pct": 0.5},\n    )\n\n\ndef test_spawn_does_not_invent_missing_meta_pass():\n    ex = ChildPolicyExecutor(_spawn_config())\n    obs = {"ts": datetime(2026,7,28,13,31,tzinfo=UTC), "symbol":"NVDA", "strategy":"momentum", "direction":"long", "price":100.0, "stop_loss":99.0, "take_profit":102.0, "champion_candidate":{"id":"c1","ml_probability":0.9,"trade_score":90.0,"final_decision":"rejected"}}\n    event = ex.evaluate_observation(obs)\n    assert event["meta_decision"] == "UNAVAILABLE"\n    assert event["hypothetical_final_decision"] == "REJECTED"\n    assert event["virtual_order_id"] is None\n\n\ndef test_spawn_does_not_invent_missing_stop():\n    ex = ChildPolicyExecutor(_spawn_config())\n    obs = {"ts": datetime(2026,7,28,13,31,tzinfo=UTC), "symbol":"NVDA", "strategy":"momentum", "direction":"long", "price":100.0, "take_profit":102.0, "champion_candidate":{"id":"c2","ml_probability":0.9,"meta_probability":0.9,"trade_score":90.0,"final_decision":"accepted"}}\n    event = ex.evaluate_observation(obs)\n    assert event["stop_loss"] is None\n    assert event["risk_decision"] == "UNAVAILABLE_MISSING_OR_INVALID_STOP"\n    assert event["virtual_order_id"] is None\n\n\ndef test_research_tables_do_not_touch_orders_or_trades(tmp_path):\n    db = Database(tmp_path / "research-integrity.db")\n    now = datetime(2026, 7, 28, 13, 31, tzinfo=UTC)\n    db.save_harvest_shadow_state("paper:A", now, "AAPL", "paper", {"symbol": "AAPL"})\n    db.save_target_d_observation({"event_id": "e", "symbol": "AAPL", "strategy": "orb", "ts": now, "target_d_decision": "UNAVAILABLE"})\n    with db.engine.connect() as conn:\n        assert conn.exec_driver_sql("SELECT COUNT(*) FROM orders").scalar() == 0\n        assert conn.exec_driver_sql("SELECT COUNT(*) FROM trades").scalar() == 0\n''', encoding="utf-8")

model = ROOT / "backend/app/ai/models/target_d_shadow_challenger.joblib"
source = ROOT / "scratch/ml_alignment_research/strategy_candidate_events.parquet"
print("Applied complete existing-shadow correctness/observability patch.")
print("Changed: database.py, engine.py, target_d_observer.py, shadow_runtime.py, spawn_executor.py, spawn_shadow_coordinator.py, shadow_event_publisher.py")
print("Added tests: backend/tests/test_research_evidence_integrity_20260728.py")
if model.exists():
    print("TARGET_D_ARTIFACT=READY")
elif source.exists():
    print("TARGET_D_ARTIFACT=MISSING; deterministic frozen builder source is present.")
    print("NEXT: python scripts/build_target_d_shadow_artifact.py")
else:
    print("TARGET_D_ARTIFACT=MISSING; frozen training parquet is also absent. Do NOT fabricate/retrain from newer data.")
