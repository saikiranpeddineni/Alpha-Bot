"""Unified persistence boundary for shadow evidence keyed by candidate and challenger.

Research persistence is deliberately fail-open. This module has no broker, portfolio,
risk-manager, or execution dependency and never returns a value used by Champion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from app.core.logging import applog
from app.db.database import Database
from app.research.shadow_event import TradingObservationEvent


@dataclass(frozen=True)
class EvidenceIdentity:
    candidate_id: str
    challenger_id: str
    challenger_version: str

    @property
    def key(self) -> str:
        return f"{self.candidate_id}|{self.challenger_id}|{self.challenger_version}"


class UnifiedEvidenceStore:
    """Idempotent research-only evidence store with restart-safe watermarks.

    The database setting is a compatibility persistence layer until a dedicated table
    migration is introduced. Writes happen only from the shadow worker, never from the
    Champion decision path. Any persistence error is counted and swallowed.
    """

    SETTING_KEY = "shadow_unified_evidence_v1"
    WATERMARK_KEY = "shadow_unified_evidence_watermarks_v1"

    def __init__(self, db: Database | None, *, max_records: int = 20000) -> None:
        self.db = db
        self.max_records = max(100, int(max_records))
        self._lock = Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._event_watermarks: set[str] = set()
        self.persistence_failures = 0
        self.duplicate_writes = 0
        self.records_written = 0
        self.last_persisted_at: datetime | None = None
        self._restore()

    def _restore(self) -> None:
        if self.db is None:
            return
        try:
            raw = self.db.get_setting(self.SETTING_KEY) or {}
            watermarks = self.db.get_setting(self.WATERMARK_KEY) or []
            if isinstance(raw, dict):
                self._records = dict(raw)
            if isinstance(watermarks, list):
                self._event_watermarks = {str(item) for item in watermarks}
        except Exception as exc:
            self.persistence_failures += 1
            applog.warning("shadow_evidence", f"Evidence restore failed open: {exc}")

    def record(
        self,
        event: TradingObservationEvent,
        *,
        challenger_id: str,
        challenger_version: str,
        decision: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        if not event.candidate_id:
            return False
        identity = EvidenceIdentity(event.candidate_id, challenger_id, challenger_version)
        watermark = f"{event.event_id}|{identity.key}"
        with self._lock:
            if watermark in self._event_watermarks:
                self.duplicate_writes += 1
                return True
            record = {
                "candidate_id": event.candidate_id,
                "event_id": event.event_id,
                "challenger_id": challenger_id,
                "challenger_version": challenger_version,
                "decision": decision,
                "event_ts": event.ts.isoformat(),
                "decision_bar_timestamp_utc": event.decision_bar_timestamp_utc.isoformat() if event.decision_bar_timestamp_utc else None,
                "symbol": event.symbol,
                "strategy": event.strategy,
                "champion_final_decision": event.champion_final_decision,
                "schema_version": event.schema_version,
                "provenance": event.provenance.model_dump(mode="json"),
                "evidence": evidence or {},
            }
            # Validate serializability before mutating in-memory state.
            json.dumps(record, sort_keys=True)
            self._records[identity.key] = record
            self._event_watermarks.add(watermark)
            self.records_written += 1
            if len(self._records) > self.max_records:
                oldest_key = next(iter(self._records))
                self._records.pop(oldest_key, None)
            self._persist_fail_open()
            return True

    def _persist_fail_open(self) -> None:
        if self.db is None:
            return
        try:
            self.db.set_setting(self.SETTING_KEY, self._records)
            self.db.set_setting(self.WATERMARK_KEY, sorted(self._event_watermarks)[-self.max_records:])
            self.last_persisted_at = datetime.now(timezone.utc)
        except Exception as exc:
            self.persistence_failures += 1
            applog.warning("shadow_evidence", f"Evidence persistence failed open: {exc}")

    def get(self, candidate_id: str, challenger_id: str, challenger_version: str) -> dict[str, Any] | None:
        return self._records.get(EvidenceIdentity(candidate_id, challenger_id, challenger_version).key)

    def telemetry(self) -> dict[str, Any]:
        return {
            "records": len(self._records),
            "records_written": self.records_written,
            "duplicate_writes": self.duplicate_writes,
            "persistence_failures": self.persistence_failures,
            "watermarks": len(self._event_watermarks),
            "last_persisted_at": self.last_persisted_at.isoformat() if self.last_persisted_at else None,
        }
