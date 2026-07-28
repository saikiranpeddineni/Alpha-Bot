import asyncio
from datetime import UTC, datetime

from app.research.shadow_event import ShadowEventType, TradingObservationEvent
from app.research.shadow_runtime import ShadowResearchRuntime


class MemoryDb:
    def __init__(self):
        self.values = {}

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        self.values[key] = value


def candidate(event_id="candidate-evt"):
    return TradingObservationEvent(
        event_type=ShadowEventType.CANDIDATE,
        event_id=event_id,
        candidate_id="cand-1",
        ts=datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
        decision_bar_timestamp_utc=datetime(2026, 7, 24, 14, 29, tzinfo=UTC),
        symbol="NVDA",
        side="long",
        strategy="momentum",
        ml_probability=0.61,
        ml_decision="PASS",
        champion_final_decision="accepted",
    )


def test_runtime_exposes_no_execution_authority():
    runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)
    forbidden = {"broker", "portfolio", "risk", "submit", "cancel_order", "close_position"}
    assert forbidden.isdisjoint(vars(runtime))
    assert forbidden.isdisjoint(vars(runtime.publisher))


def test_publish_before_start_fails_open():
    runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)
    assert runtime.publish(candidate()) is False


def test_candidate_fanout_records_champion_evidence():
    async def run():
        db = MemoryDb()
        runtime = ShadowResearchRuntime(db=db, gemini_api_key=None)
        await runtime.start()
        try:
            assert runtime.publish(candidate()) is True
            await asyncio.wait_for(runtime.publisher.queue.join(), timeout=1.0)
            record = runtime.evidence.get("cand-1", "champion", "production")
            assert record is not None
            assert record["decision"] == "accepted"
            assert runtime.publisher.events_processed == 1
        finally:
            await runtime.stop()

    asyncio.run(run())


def test_legacy_shadow_ledger_is_reachable_but_canonical_joiner_owns_outcomes():
    async def run():
        runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)
        await runtime.start()
        try:
            assert runtime.publish(candidate()) is True
            await asyncio.wait_for(runtime.publisher.queue.join(), timeout=1.0)
            telemetry = runtime.telemetry()
            assert telemetry["module_events"]["legacy_shadow_ledger"] == 1
            assert telemetry["outcomes"]["candidates_seen"] == 1
        finally:
            await runtime.stop()

    asyncio.run(run())


def test_discovery_session_is_reachable_through_common_audit_plane():
    runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)
    runtime.record_discovery_session("2026-07-28", {
        "fixed_candidates": ["AAPL"],
        "dynamic_candidates": ["AAPL", "NVDA"],
        "fixed_qualified": ["AAPL"],
        "dynamic_qualified": ["AAPL", "NVDA"],
        "fixed_requests": 1,
        "dynamic_requests": 1,
        "fixed_latency_ms": 10.0,
        "dynamic_latency_ms": 12.0,
        "dynamic_missing": 0,
        "dynamic_stale": 0,
    })
    assert runtime.telemetry()["module_events"]["discovery_validation"] == 1


def test_target_d_evidence_uses_same_candidate_identity():
    runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)
    runtime.target_d.consume = lambda event: {
        "candidate_id": event.candidate_id,
        "event_id": event.event_id,
        "target_d_probability": 0.72,
        "target_d_decision": "PASS",
        "disagreement_category": "AGREE_PASS",
    }
    evt = candidate()
    runtime._consume_target_d(evt)
    record = runtime.evidence.get("cand-1", "target_d", "v1")
    assert record is not None
    assert record["candidate_id"] == "cand-1"
    assert record["event_id"] == "candidate-evt"
    assert record["decision"] == "PASS"
    assert record["evidence"]["target_d_probability"] == 0.72


def test_duplicate_candidate_is_single_fanout():
    async def run():
        runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)
        await runtime.start()
        try:
            evt = candidate()
            assert runtime.publish(evt) is True
            assert runtime.publish(evt) is True
            await asyncio.wait_for(runtime.publisher.queue.join(), timeout=1.0)
            assert runtime.publisher.events_published == 1
            assert runtime.publisher.events_duplicate == 1
            assert runtime.evidence.get("cand-1", "champion", "production") is not None
        finally:
            await runtime.stop()

    asyncio.run(run())


def test_spawn_start_failure_is_contained():
    async def run():
        runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)

        async def broken_start():
            raise RuntimeError("spawn unavailable")

        runtime.spawn.start = broken_start
        await runtime.start()
        try:
            assert runtime.publish(candidate()) is True
            await asyncio.wait_for(runtime.publisher.queue.join(), timeout=1.0)
            assert runtime.evidence.get("cand-1", "champion", "production") is not None
            assert runtime.telemetry()["lifecycle_errors"] == 1
        finally:
            await runtime.stop()

    asyncio.run(run())


def test_shutdown_failures_are_all_contained():
    async def run():
        runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)
        await runtime.start()

        async def broken_shutdown():
            raise RuntimeError("research cleanup failed")

        runtime.gemini.shutdown = broken_shutdown
        runtime.spawn.stop = broken_shutdown
        await runtime.stop()
        assert runtime.telemetry()["started"] is False
        assert runtime.telemetry()["lifecycle_errors"] == 2

    asyncio.run(run())


def test_publish_exception_is_contained():
    runtime = ShadowResearchRuntime(db=MemoryDb(), gemini_api_key=None)

    def broken_publish(_event):
        raise RuntimeError("queue failure")

    runtime.publisher.publish = broken_publish
    assert runtime.publish(candidate()) is False
    assert runtime.telemetry()["lifecycle_errors"] == 1
