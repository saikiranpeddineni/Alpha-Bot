from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/app/db/database.py"
ENGINE = ROOT / "backend/app/core/engine.py"
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
    '''        # SQLite is shared by the production engine, API readers, durable fill\n        # ledger, and research writers.  Give transient writer contention time to\n        # clear instead of surfacing an immediate "database is locked" failure.\n        # WAL keeps readers from unnecessarily blocking the single SQLite writer.\n        self.engine = create_engine(\n            f"sqlite:///{path}",\n            connect_args={"check_same_thread": False, "timeout": 30.0},\n        )\n        with self.engine.connect() as conn:\n            conn.exec_driver_sql("PRAGMA busy_timeout=30000")\n            conn.exec_driver_sql("PRAGMA journal_mode=WAL")\n            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")\n        Base.metadata.create_all(self.engine)''',
    "sqlite contention hardening",
)

db = replace_once(
    db,
    '\n\nclass ContextualPerformanceMemoryRow(Base):',
    '''\n\nclass HarvestShadowStateRow(Base):\n    """Durable snapshot of one Harvest-V2 counterfactual observation.\n\n    This table is research-only.  It records the passive observer state and has\n    no production/broker authority.  One row is upserted per observed position.\n    """\n    __tablename__ = "harvest_shadow_states"\n    id: Mapped[str] = mapped_column(String(160), primary_key=True)\n    ts: Mapped[datetime]\n    symbol: Mapped[str] = mapped_column(String(12))\n    mode: Mapped[str] = mapped_column(String(10))\n    payload: Mapped[str] = mapped_column(Text)\n\n\nclass ContextualPerformanceMemoryRow(Base):''',
    "harvest shadow table",
)

db = replace_once(
    db,
    '    # ---- durable broker fill / portfolio checkpoint contract ----',
    '''    # ---- passive Harvest-V2 research evidence ----\n\n    def save_harvest_shadow_state(\n        self, observation_id: str, ts: datetime, symbol: str, mode: str, payload: dict\n    ) -> None:\n        """Upsert passive Harvest-V2 evidence without changing production state."""\n        with self._session() as s:\n            s.merge(\n                HarvestShadowStateRow(\n                    id=observation_id,\n                    ts=ts,\n                    symbol=symbol.upper(),\n                    mode=mode,\n                    payload=json.dumps(payload, default=str, sort_keys=True),\n                )\n            )\n            s.commit()\n\n    def get_harvest_shadow_states(self, mode: str | None = None) -> list[dict]:\n        with self._session() as s:\n            stmt = select(HarvestShadowStateRow).order_by(HarvestShadowStateRow.ts.asc())\n            if mode:\n                stmt = stmt.where(HarvestShadowStateRow.mode == mode)\n            return [json.loads(row.payload) for row in s.scalars(stmt)]\n\n    # ---- durable broker fill / portfolio checkpoint contract ----''',
    "harvest persistence methods",
)

DB.write_text(db, encoding="utf-8")

engine = ENGINE.read_text(encoding="utf-8")
engine = replace_once(
    engine,
    '''                st["winner_capture"] = (st["shadow_realized_r"] / st["mfe_r"]) if st["shadow_realized_r"] > 0 and st["mfe_r"] > 0 else 0.0\n\n        except Exception as exc:''',
    '''                st["winner_capture"] = (st["shadow_realized_r"] / st["mfe_r"]) if st["shadow_realized_r"] > 0 and st["mfe_r"] > 0 else 0.0\n\n            # Research-only durability.  Persist the latest causal snapshot for\n            # this observed position.  Failure to record research evidence must\n            # never alter Champion decisions, exits, sizing, or broker authority.\n            try:\n                observation_id = f"{self.mode}:{symbol}:{st['observation_started_at']}"\n                persist_ts = current_bar_ts.to_pydatetime() if hasattr(current_bar_ts, "to_pydatetime") else current_bar_ts\n                self.db.save_harvest_shadow_state(\n                    observation_id=observation_id,\n                    ts=persist_ts,\n                    symbol=symbol,\n                    mode=self.mode,\n                    payload=dict(st),\n                )\n            except Exception as persist_exc:\n                applog.warning(\n                    "engine.v2_shadow",\n                    f"V2 shadow evidence persistence failed for {symbol}: {persist_exc}",\n                )\n\n        except Exception as exc:''',
    "harvest engine persistence",
)
ENGINE.write_text(engine, encoding="utf-8")

TEST.write_text('''from __future__ import annotations\n\nfrom datetime import UTC, datetime\n\nfrom app.db.database import Database\n\n\ndef test_sqlite_runtime_uses_wal_and_busy_timeout(tmp_path):\n    db = Database(tmp_path / "research-integrity.db")\n    with db.engine.connect() as conn:\n        assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal"\n        assert int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar()) >= 30000\n\n\ndef test_harvest_shadow_state_is_durable_and_upserts(tmp_path):\n    path = tmp_path / "research-integrity.db"\n    db = Database(path)\n    ts1 = datetime(2026, 7, 28, 13, 31, tzinfo=UTC)\n    ts2 = datetime(2026, 7, 28, 13, 32, tzinfo=UTC)\n    observation_id = "paper:NVDA:2026-07-28T13:30:00+00:00"\n\n    db.save_harvest_shadow_state(\n        observation_id, ts1, "NVDA", "paper",\n        {"symbol": "NVDA", "harvest_state": "runner_preserve", "mfe_r": 0.5},\n    )\n    db.save_harvest_shadow_state(\n        observation_id, ts2, "NVDA", "paper",\n        {"symbol": "NVDA", "harvest_state": "confirmed_giveback", "mfe_r": 1.8, "shadow_realized_r": 1.2},\n    )\n\n    db2 = Database(path)\n    rows = db2.get_harvest_shadow_states("paper")\n    assert len(rows) == 1\n    assert rows[0]["harvest_state"] == "confirmed_giveback"\n    assert rows[0]["mfe_r"] == 1.8\n    assert rows[0]["shadow_realized_r"] == 1.2\n\n\ndef test_harvest_evidence_is_mode_isolated(tmp_path):\n    db = Database(tmp_path / "research-integrity.db")\n    now = datetime(2026, 7, 28, 13, 31, tzinfo=UTC)\n    db.save_harvest_shadow_state("paper:A", now, "AAPL", "paper", {"symbol": "AAPL"})\n    db.save_harvest_shadow_state("live:A", now, "AAPL", "live", {"symbol": "AAPL"})\n    assert len(db.get_harvest_shadow_states("paper")) == 1\n    assert len(db.get_harvest_shadow_states("live")) == 1\n''', encoding="utf-8")

print("Applied research evidence integrity patch.")
print("Changed: backend/app/db/database.py, backend/app/core/engine.py")
print("Added: backend/tests/test_research_evidence_integrity_20260728.py")
