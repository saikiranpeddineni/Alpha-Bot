import asyncio
from datetime import UTC, datetime, timedelta

from app.research.module_audit_store import ShadowModuleAuditStore
from app.research.outcome_joiner import CanonicalOutcomeJoiner
from app.research.shadow_event import ShadowEventType, TradingObservationEvent


class AuditDb:
    def __init__(self):
        self.rows = {}

    def get_policy_evidence(self, key):
        return self.rows.get(key)

    def save_policy_evidence(self, evidence_id, policy_id, ts, payload):
        if evidence_id in self.rows:
            raise AssertionError("duplicate insert")
        self.rows[evidence_id] = payload


def _candidate(*, stop=99.0):
    return TradingObservationEvent(
        event_type=ShadowEventType.CANDIDATE, event_id="evt-c1", candidate_id="c1",
        ts=datetime(2026, 7, 28, 13, 30, tzinfo=UTC), symbol="XYZ", side="long",
        strategy="momentum", price=100.0, stop_loss=stop, atr=1.0,
        champion_final_decision="accepted",
    )


def _bar(minutes, *, open_=100.0, high=101.0, low=99.5, close=100.5):
    return TradingObservationEvent(
        event_type=ShadowEventType.MARKET_BAR, event_id=f"bar-{minutes}",
        ts=datetime(2026, 7, 28, 13, 30, tzinfo=UTC) + timedelta(minutes=minutes),
        symbol="XYZ", open=open_, high=high, low=low, close=close, atr=1.0,
    )


def test_module_audit_is_collision_safe_and_idempotent():
    db = AuditDb()
    store = ShadowModuleAuditStore(db)
    long_identity = "c" * 64 + ":policy-a"
    assert store.record("exit_policy", {"x": 1}, identity=long_identity)
    assert store.record("exit_policy", {"x": 2}, identity=long_identity)
    assert len(db.rows) == 1
    assert store.duplicate_writes == 1


def test_outcome_joiner_prices_gap_through_stop_not_minus_one_r():
    outputs = []
    joiner = CanonicalOutcomeJoiner(lambda event, horizon, outcome: outputs.append(outcome))
    joiner.consume(_candidate(stop=99.0))
    joiner.consume(_bar(1, open_=97.0, high=98.0, low=96.5, close=97.5))
    assert outputs
    assert outputs[0]["stopped"] is True
    assert outputs[0]["stop_fill_price"] == 97.0
    assert outputs[0]["realized_r"] == -3.0


def test_outcome_joiner_rejects_duplicate_or_late_bar_per_candidate():
    joiner = CanonicalOutcomeJoiner(lambda *_: None)
    joiner.consume(_candidate())
    bar = _bar(1)
    joiner.consume(bar)
    joiner.consume(bar)
    assert joiner.out_of_order_bars == 1


def test_exit_consumer_includes_harvest_v2_in_same_causal_replay():
    from app.research.exit_challenger_consumer import CanonicalExitChallengerConsumer

    outputs = []
    consumer = CanonicalExitChallengerConsumer(lambda event, policy, evidence: outputs.append((policy, evidence)))
    consumer.consume(_candidate())
    # One 60-minute causal bar is enough to trigger the consolidated replay.  The
    # replay path itself contains only observed post-entry data; no future fetch occurs.
    consumer.consume(_bar(60, open_=100.0, high=102.0, low=99.5, close=101.0))
    policies = {name for name, _ in outputs}
    assert "harvest_v2_giveback_confirmed" in policies
    assert "fixed_1_2" in policies
    assert consumer.harvest_replays == 1
