"""Fail-open durable audit trail for ShadowAlphaBot module activity.

This store deliberately depends only on the database persistence boundary.  It has
no broker, risk, portfolio, scanner, or production-decision authority.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from app.core.logging import applog
from app.db.database import Database


class ShadowModuleAuditStore:
    """Persist module outputs/telemetry without allowing research failures into PROD."""

    def __init__(self, db: Database | None) -> None:
        self.db = db
        self._lock = Lock()
        self.records_written = 0
        self.persistence_failures = 0

    def record(self, module: str, payload: dict[str, Any], *, identity: str | None = None,
               ts: datetime | None = None) -> bool:
        if self.db is None or not hasattr(self.db, "save_policy_evidence"):
            return False
        when = ts or datetime.now(UTC)
        body = {
            "schema_version": 1,
            "research_only": True,
            "module": module,
            "recorded_at": when.isoformat(),
            **payload,
        }
        try:
            encoded = json.dumps(body, sort_keys=True, default=str, allow_nan=False)
            stable = identity or hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
            evidence_id = f"shadow:{module}:{stable}"[:64]
            with self._lock:
                self.db.save_policy_evidence(evidence_id, f"shadow:{module}"[:64], when, body)
            self.records_written += 1
            return True
        except Exception as exc:
            # Duplicate/idempotent writes and all persistence faults are non-authoritative.
            self.persistence_failures += 1
            applog.warning("shadow_audit", f"{module} audit persistence failed open: {exc}")
            return False

    def telemetry(self) -> dict[str, int]:
        return {"records_written": self.records_written, "persistence_failures": self.persistence_failures}
