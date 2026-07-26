from datetime import UTC, datetime

from app.research.evidence_store import UnifiedEvidenceStore
from app.research.shadow_event import ShadowEventType, TradingObservationEvent


class LockedDb:
    def get_setting(self, _key):
        raise RuntimeError("database is locked")

    def set_setting(self, _key, _value):
        raise RuntimeError("database is locked")


class MemoryDb:
    def __init__(self):
        self.values = {}

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        self.values[key] = value


def event(event_id="evt-1"):
    return TradingObservationEvent(
        event_type=ShadowEventType.CANDIDATE,
        event_id=event_id,
        candidate_id="cand-1",
        ts=datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
        decision_bar_timestamp_utc=datetime(2026, 7, 24, 14, 29, tzinfo=UTC),
        symbol="NVDA",
        side="long",
        strategy="momentum",
        champion_final_decision="accepted",
    )


def test_locked_db_is_research_failure_not_exception():
    store = UnifiedEvidenceStore(LockedDb())
    assert store.persistence_failures == 1
    assert store.record(event(), challenger_id="champion", challenger_version="production", decision="accepted") is True
    assert store.persistence_failures == 2
    assert store.get("cand-1", "champion", "production")["decision"] == "accepted"


def test_duplicate_event_challenger_identity_is_idempotent():
    store = UnifiedEvidenceStore(MemoryDb())
    assert store.record(event(), challenger_id="target_d", challenger_version="v1", decision="PASS") is True
    assert store.record(event(), challenger_id="target_d", challenger_version="v1", decision="PASS") is True
    assert store.records_written == 1
    assert store.duplicate_writes == 1


def test_restart_restores_evidence_without_production_state():
    db = MemoryDb()
    first = UnifiedEvidenceStore(db)
    first.record(event(), challenger_id="champion", challenger_version="production", decision="accepted")
    second = UnifiedEvidenceStore(db)
    restored = second.get("cand-1", "champion", "production")
    assert restored is not None
    assert restored["champion_final_decision"] == "accepted"
    assert not hasattr(second, "broker")
    assert not hasattr(second, "portfolio")
