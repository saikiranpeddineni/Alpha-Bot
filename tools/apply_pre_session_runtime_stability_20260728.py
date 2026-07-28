"""Apply pre-session runtime stability hardening without changing trading decisions.

Idempotent source patch for:
1. SQLite WAL/busy-timeout hardening to reduce writer lock failures under shadow load.
2. Shadow unified-evidence persistence throttling to avoid rewriting a large settings
   blob on every module observation. ModuleAuditStore remains the per-event durable
   evidence trail; unified evidence is checkpointed periodically and on shutdown.
3. Persist the production portfolio session baseline immediately after a true session
   roll so a later process restart cannot reset the daily-loss baseline.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    src = path.read_text(encoding="utf-8")
    if new in src:
        print(f"already applied: {label}")
        return
    if old not in src:
        raise RuntimeError(f"anchor not found for {label}: {path}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    print(f"applied: {label}")


# SQLite: wait for transient writer contention and use WAL so dashboard/research reads
# do not unnecessarily block the trading writer.
db = ROOT / "backend/app/db/database.py"
replace_once(
    db,
    'self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})\n        Base.metadata.create_all(self.engine)',
    'self.engine = create_engine(\n            f"sqlite:///{path}",\n            connect_args={"check_same_thread": False, "timeout": 30.0},\n        )\n        with self.engine.connect() as conn:\n            conn.exec_driver_sql("PRAGMA journal_mode=WAL")\n            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")\n            conn.exec_driver_sql("PRAGMA busy_timeout=30000")\n        Base.metadata.create_all(self.engine)',
    "sqlite WAL/busy timeout",
)

# UnifiedEvidenceStore: the durable per-module audit already writes every event to
# policy_evidence. Avoid O(N) JSON serialization + two SQLite writes for every shadow
# observation; checkpoint this compatibility snapshot in bounded batches instead.
evidence = ROOT / "backend/app/research/evidence_store.py"
replace_once(
    evidence,
    'def __init__(self, db: Database | None, *, max_records: int = 20000) -> None:\n        self.db = db\n        self.max_records = max(100, int(max_records))',
    'def __init__(self, db: Database | None, *, max_records: int = 20000, persist_every: int = 50) -> None:\n        self.db = db\n        self.max_records = max(100, int(max_records))\n        self.persist_every = max(1, int(persist_every))\n        self._dirty_records = 0',
    "bounded unified-evidence persistence",
)
replace_once(
    evidence,
    '            self._persist_fail_open()\n            return True',
    '            self._dirty_records += 1\n            if self._dirty_records >= self.persist_every:\n                self._persist_fail_open()\n            return True',
    "unified-evidence checkpoint cadence",
)
replace_once(
    evidence,
    '            self.last_persisted_at = datetime.now(timezone.utc)\n        except Exception as exc:',
    '            self.last_persisted_at = datetime.now(timezone.utc)\n            self._dirty_records = 0\n        except Exception as exc:',
    "unified-evidence dirty reset",
)
replace_once(
    evidence,
    '    def get(self, candidate_id: str, challenger_id: str, challenger_version: str) -> dict[str, Any] | None:',
    '    def flush(self) -> None:\n        """Persist any pending compatibility snapshot; failures remain research-only."""\n        if self._dirty_records:\n            self._persist_fail_open()\n\n    def get(self, candidate_id: str, challenger_id: str, challenger_version: str) -> dict[str, Any] | None:',
    "unified-evidence explicit flush",
)

# Flush the batched compatibility evidence before the research runtime stops.
runtime = ROOT / "backend/app/research/shadow_runtime.py"
replace_once(
    runtime,
    '    async def stop(self) -> None:\n        self.audit.record("runtime", {"event": "STOP", "telemetry": self.telemetry()})',
    '    async def stop(self) -> None:\n        self.audit.record("runtime", {"event": "STOP", "telemetry": self.telemetry()})\n        self.evidence.flush()',
    "shadow evidence shutdown flush",
)

# Persist a newly-established production session baseline immediately. This is
# restart-safety only and does not alter strategy/risk decisions in the running process.
engine = ROOT / "backend/app/core/engine.py"
replace_once(
    engine,
    '        if self.portfolio.roll_session(session, account.equity):\n            self.risk.roll_session(session)\n            self.insights.on_performance(self.portfolio.stats(), 0.0)',
    '        if self.portfolio.roll_session(session, account.equity):\n            self.risk.roll_session(session)\n            if self.mode in {"paper", "live"}:\n                try:\n                    account_id = getattr(self.broker, "account_id", None)\n                    self.db.save_portfolio_checkpoint(\n                        self.mode,\n                        self.portfolio.checkpoint(\n                            account_id=account_id,\n                            risk_state=self.risk.get_risk_state(),\n                        ),\n                    )\n                except Exception as exc:\n                    applog.error("engine", f"Failed to persist session-roll checkpoint: {exc}")\n            self.insights.on_performance(self.portfolio.stats(), 0.0)',
    "durable session-roll baseline",
)

print("Pre-session runtime stability patch complete.")
