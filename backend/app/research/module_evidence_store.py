"""Durable append-only audit store for every ShadowAlphaBot research module.

This store is research-only. It has no broker/risk/portfolio dependency and every
write is fail-open so persistence can never affect Champion execution.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from app.core.logging import applog
from app.db.database import Database


class ShadowModuleEvidenceStore:
    """SQLite-backed, idempotent module audit trail.

    A dedicated table is created lazily so existing databases self-heal without a
    destructive migration. Payloads are immutable JSON snapshots and are never read
    by the production decision path.
    """

    TABLE = "shadow_module_evidence"

    def __init__(self, db: Database | None) -> None:
        self.db = db
        self._lock = Lock()
        self.records_written = 0
        self.duplicate_writes = 0
        self.persistence_failures = 0
        self.last_persisted_at: datetime | None = None
        self._ensure_table()

    def _ensure_table(self) -> None:
        if self.db is None:
            return
        try:
            with self.db.engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS shadow_module_evidence ("
                    "id VARCHAR(64) PRIMARY KEY, ts DATETIME NOT NULL, "
                    "module VARCHAR(64) NOT NULL, event_type VARCHAR(32) NOT NULL, "
                    "candidate_id VARCHAR(128), event_id VARCHAR(128), symbol VARCHAR(20), "
                    "strategy VARCHAR(64), decision VARCHAR(64), schema_version INTEGER NOT NULL, "
                    "payload_json TEXT NOT NULL)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_shadow_module_evidence_module_ts "
                    "ON shadow_module_evidence(module, ts)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_shadow_module_evidence_candidate "
                    "ON shadow_module_evidence(candidate_id)"
                )
        except Exception as exc:
            self.persistence_failures += 1
            applog.warning("shadow_module_evidence", f"table initialization failed open: {exc}")

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)

    def record(self, *, module: str, event_type: str, ts: datetime,
               payload: dict[str, Any], candidate_id: str | None = None,
               event_id: str | None = None, symbol: str | None = None,
               strategy: str | None = None, decision: str | None = None,
               identity_suffix: str | None = None) -> bool:
        if not module or not event_type:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        body = self._json(payload)
        identity = "|".join([
            module, event_type, event_id or "", candidate_id or "", identity_suffix or "",
            ts.isoformat(), hashlib.sha256(body.encode()).hexdigest(),
        ])
        row_id = hashlib.sha256(identity.encode()).hexdigest()
        if self.db is None:
            return False
        try:
            with self._lock, self.db.engine.begin() as conn:
                result = conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO shadow_module_evidence "
                    "(id,ts,module,event_type,candidate_id,event_id,symbol,strategy,decision,schema_version,payload_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (row_id, ts, module, event_type, candidate_id, event_id, symbol, strategy,
                     decision, 1, body),
                )
                if result.rowcount == 0:
                    self.duplicate_writes += 1
                else:
                    self.records_written += 1
                    self.last_persisted_at = datetime.now(timezone.utc)
            return True
        except Exception as exc:
            self.persistence_failures += 1
            applog.warning("shadow_module_evidence", f"write failed open for {module}: {exc}")
            return False

    def telemetry(self) -> dict[str, Any]:
        rows = 0
        modules: dict[str, int] = {}
        if self.db is not None:
            try:
                with self.db.engine.connect() as conn:
                    rows = int(conn.exec_driver_sql("SELECT COUNT(*) FROM shadow_module_evidence").scalar() or 0)
                    for module, count in conn.exec_driver_sql(
                        "SELECT module,COUNT(*) FROM shadow_module_evidence GROUP BY module"
                    ).fetchall():
                        modules[str(module)] = int(count)
            except Exception:
                pass
        return {
            "records": rows,
            "by_module": modules,
            "records_written": self.records_written,
            "duplicate_writes": self.duplicate_writes,
            "persistence_failures": self.persistence_failures,
            "last_persisted_at": self.last_persisted_at.isoformat() if self.last_persisted_at else None,
        }
