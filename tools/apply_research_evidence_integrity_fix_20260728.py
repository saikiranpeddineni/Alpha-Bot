from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/app/db/database.py"
ENGINE = ROOT / "backend/app/core/engine.py"
TARGET_D = ROOT / "backend/app/ai/target_d_observer.py"
RUNTIME = ROOT / "backend/app/research/shadow_runtime.py"
TEST = ROOT / "backend/tests/test_research_evidence_integrity_20260728.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"anchor not found for {label}")
    return text.replace(old, new, 1)


db = DB.read_text(encoding="utf-8")
db = replace_once(
    db,
    '        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})\n        Base.metadata.create_all(self.engine)',
    '''        # SQLite is shared by production and passive research writers.  Wait for\n        # transient writer contention rather than immediately surfacing "database is locked".\n        self.engine = create_engine(\n            f"sqlite:///{path}",\n            connect_args={"check_same_thread": False, "timeout": 30.0},\n        )\n        with self.engine.connect() as conn:\n            conn.exec_driver_sql("PRAGMA busy_timeout=30000")\n            conn.exec_driver_sql("PRAGMA journal_mode=WAL")\n            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")\n        Base.metadata.create_all(self.engine)''',
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

engine = ENGINE.read_text(encoding="utf-8")
engine = replace_once(
    engine,
    '''                st["winner_capture"] = (st["shadow_realized_r"] / st["mfe_r"]) if st["shadow_realized_r"] > 0 and st["mfe_r"] > 0 else 0.0\n\n        except Exception as exc:''',
    '''                st["winner_capture"] = (st["shadow_realized_r"] / st["mfe_r"]) if st["shadow_realized_r"] > 0 and st["mfe_r"] > 0 else 0.0\n\n            # Passive research durability only; never feed this result back into Champion.\n            try:\n                observation_id = f"{self.mode}:{symbol}:{st['observation_started_at']}"\n                persist_ts = current_bar_ts.to_pydatetime() if hasattr(current_bar_ts, "to_pydatetime") else current_bar_ts\n                self.db.save_harvest_shadow_state(observation_id, persist_ts, symbol, self.mode, dict(st))\n            except Exception as persist_exc:\n                applog.warning("engine.v2_shadow", f"V2 shadow evidence persistence failed for {symbol}: {persist_exc}")\n\n        except Exception as exc:''',
    "harvest engine persistence",
)
ENGINE.write_text(engine, encoding="utf-8")

target = TARGET_D.read_text(encoding="utf-8")
target = replace_once(
    target,
    '''        self.threshold = 0.48\n        self._load_artifact()''',
    '''        self.threshold = 0.48\n        self.artifact_status = "UNINITIALIZED"\n        self.artifact_error = None\n        self._load_artifact()''',
    "target d status fields",
)
target = replace_once(
    target,
    '''        try:\n            if os.path.exists(self.model_path):\n                self.model = joblib.load(self.model_path)\n            if os.path.exists(self.meta_path):''',
    '''        try:\n            if not os.path.exists(self.model_path):\n                self.artifact_status = "MISSING_MODEL"\n                self.artifact_error = self.model_path\n                applog.warning("target_d_observer", f"Target D model unavailable: {self.model_path}")\n                return\n            self.model = joblib.load(self.model_path)\n            if os.path.exists(self.meta_path):''',
    "target d missing model diagnostics",
)
target = replace_once(
    target,
    '''                self.threshold = float(self.metadata.get("threshold", 0.48))\n        except Exception as exc:\n            applog.error("target_d_observer", f"Failed to load Target D artifact: {exc}")\n            self.model = None''',
    '''                self.threshold = float(self.metadata.get("threshold", 0.48))\n            self.artifact_status = "READY"\n            self.artifact_error = None\n        except Exception as exc:\n            self.artifact_status = "LOAD_ERROR"\n            self.artifact_error = f"{type(exc).__name__}: {exc}"\n            applog.error("target_d_observer", f"Failed to load Target D artifact: {exc}")\n            self.model = None''',
    "target d load diagnostics",
)
target = replace_once(
    target,
    '''            "generation_id": self.generation_id,\n            "disagreement_category": "UNAVAILABLE"\n        }''',
    '''            "generation_id": self.generation_id,\n            "disagreement_category": "UNAVAILABLE",\n            "artifact_status": self.artifact_status,\n            "unavailable_reason": self.artifact_error or ("FEATURES_UNAVAILABLE" if features is None else "MODEL_UNAVAILABLE"),\n        }''',
    "target d unavailable reason",
)
TARGET_D.write_text(target, encoding="utf-8")

runtime = RUNTIME.read_text(encoding="utf-8")
runtime = replace_once(
    runtime,
    '''        def target_d_sink(observation: dict[str, Any]) -> None:\n            if target_d_observation_sink is not None:\n                target_d_observation_sink(observation)''',
    '''        def target_d_sink(observation: dict[str, Any]) -> None:\n            # Dedicated durable evidence makes Target-D independently queryable.\n            # The canonical policy_evidence write below remains unchanged.\n            try:\n                db.save_target_d_observation(observation)\n            except Exception as exc:\n                applog.warning("target_d_shadow", f"Target D durable sink failed open: {exc}")\n            if target_d_observation_sink is not None:\n                target_d_observation_sink(observation)''',
    "target d durable sink",
)
RUNTIME.write_text(runtime, encoding="utf-8")

TEST.write_text('''from __future__ import annotations\n\nfrom datetime import UTC, datetime\nfrom app.ai.target_d_observer import TargetDShadowObserver\nfrom app.db.database import Database\n\n\ndef test_sqlite_runtime_uses_wal_and_busy_timeout(tmp_path):\n    db = Database(tmp_path / "research-integrity.db")\n    with db.engine.connect() as conn:\n        assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal"\n        assert int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar()) >= 30000\n\n\ndef test_harvest_shadow_state_is_durable_and_upserts(tmp_path):\n    path = tmp_path / "research-integrity.db"; db = Database(path)\n    ts1 = datetime(2026, 7, 28, 13, 31, tzinfo=UTC); ts2 = datetime(2026, 7, 28, 13, 32, tzinfo=UTC)\n    oid = "paper:NVDA:2026-07-28T13:30:00+00:00"\n    db.save_harvest_shadow_state(oid, ts1, "NVDA", "paper", {"harvest_state": "runner_preserve", "mfe_r": 0.5})\n    db.save_harvest_shadow_state(oid, ts2, "NVDA", "paper", {"harvest_state": "confirmed_giveback", "mfe_r": 1.8, "shadow_realized_r": 1.2})\n    rows = Database(path).get_harvest_shadow_states("paper")\n    assert len(rows) == 1 and rows[0]["harvest_state"] == "confirmed_giveback" and rows[0]["mfe_r"] == 1.8\n\n\ndef test_target_d_unavailable_is_explicit(tmp_path):\n    observer = TargetDShadowObserver(str(tmp_path / "missing.joblib"), str(tmp_path / "missing.json"))\n    result = observer.evaluate_passive({}, 0.7, "PASS")\n    assert result["target_d_decision"] == "UNAVAILABLE"\n    assert result["artifact_status"] == "MISSING_MODEL"\n    assert result["unavailable_reason"]\n\n\ndef test_target_d_observation_is_durable(tmp_path):\n    path = tmp_path / "research-integrity.db"; db = Database(path)\n    obs = {"event_id": "evt-1", "candidate_id": "cand-1", "symbol": "NVDA", "strategy": "momentum", "ts": "2026-07-28T13:31:00+00:00", "target_d_decision": "UNAVAILABLE", "artifact_status": "MISSING_MODEL"}\n    db.save_target_d_observation(obs)\n    rows = Database(path).get_target_d_observations()\n    assert len(rows) == 1 and rows[0]["candidate_id"] == "cand-1" and rows[0]["artifact_status"] == "MISSING_MODEL"\n\n\ndef test_research_tables_do_not_touch_orders_or_trades(tmp_path):\n    db = Database(tmp_path / "research-integrity.db")\n    now = datetime(2026, 7, 28, 13, 31, tzinfo=UTC)\n    db.save_harvest_shadow_state("paper:A", now, "AAPL", "paper", {"symbol": "AAPL"})\n    db.save_target_d_observation({"event_id": "e", "symbol": "AAPL", "strategy": "orb", "ts": now, "target_d_decision": "UNAVAILABLE"})\n    with db.engine.connect() as conn:\n        assert conn.exec_driver_sql("SELECT COUNT(*) FROM orders").scalar() == 0\n        assert conn.exec_driver_sql("SELECT COUNT(*) FROM trades").scalar() == 0\n''', encoding="utf-8")

model = ROOT / "backend/app/ai/models/target_d_shadow_challenger.joblib"
source = ROOT / "scratch/ml_alignment_research/strategy_candidate_events.parquet"
print("Applied research evidence integrity patch.")
print("Changed: database.py, engine.py, target_d_observer.py, shadow_runtime.py")
print("Added tests: backend/tests/test_research_evidence_integrity_20260728.py")
if model.exists():
    print("TARGET_D_ARTIFACT=READY")
elif source.exists():
    print("TARGET_D_ARTIFACT=MISSING; deterministic frozen builder source is present.")
    print("NEXT: python scripts/build_target_d_shadow_artifact.py")
else:
    print("TARGET_D_ARTIFACT=MISSING; frozen training parquet is also absent. Do NOT fabricate/retrain from newer data.")
