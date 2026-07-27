import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.database import Database, PolicyEvidenceRow
from app.research.shadow_bot import ShadowAlphaBotRuntime
from app.research.shadow_event import ShadowEventType, TradingObservationEvent
from app.research.shadow_runtime import ShadowResearchRuntime


def _candidate() -> TradingObservationEvent:
    return TradingObservationEvent(
        event_type=ShadowEventType.CANDIDATE, event_id="evt-c1", candidate_id="c1",
        ts=datetime(2026, 7, 28, 14, 30, tzinfo=UTC),
        decision_bar_timestamp_utc=datetime(2026, 7, 28, 14, 29, tzinfo=UTC),
        symbol="NVDA", side="long", strategy="momentum", price=100.0,
        stop_loss=99.0, take_profit=102.0, atr=0.5,
        ml_decision="PASS", champion_final_decision="accepted",
    )


def _bar(minute: int, close: float = 101.0) -> TradingObservationEvent:
    ts = datetime(2026, 7, 28, 14, 30, tzinfo=UTC) + timedelta(minutes=minute)
    return TradingObservationEvent(
        event_type=ShadowEventType.MARKET_BAR, event_id=f"bar-{minute}", ts=ts,
        symbol="NVDA", open=close - 0.1, high=close + 0.2, low=close - 0.2,
        close=close, atr=0.5,
    )


def _audit_rows(db: Database, policy_id: str):
    with db._session() as session:
        return session.scalars(select(PolicyEvidenceRow).where(PolicyEvidenceRow.policy_id == policy_id)).all()


def test_public_shadow_bot_has_zero_execution_capabilities(tmp_path):
    db = Database(tmp_path / "shadow.db")
    research = ShadowResearchRuntime(db=db, gemini_api_key=None)
    bot = ShadowAlphaBotRuntime(research)
    assert bot.capabilities.broker_authority is False
    assert bot.capabilities.production_mutation is False
    assert bot.capabilities.model_training is False
    assert bot.capabilities.automatic_promotion is False
    db.close()


def test_all_module_audit_is_durable_in_policy_evidence(tmp_path):
    async def run():
        db = Database(tmp_path / "shadow.db")
        runtime = ShadowResearchRuntime(db=db, gemini_api_key=None)
        await runtime.start()
        try:
            assert runtime.publish(_candidate()) is True
            await asyncio.wait_for(runtime.publisher.queue.join(), timeout=2)
            rows = _audit_rows(db, "shadow:champion")
            assert rows
            assert any("c1" in row.payload for row in rows)
        finally:
            await runtime.stop()
            db.close()
    asyncio.run(run())


def test_harvest_v2_runs_from_canonical_event_stream_and_persists(tmp_path):
    async def run():
        db = Database(tmp_path / "shadow.db")
        runtime = ShadowResearchRuntime(db=db, gemini_api_key=None)
        await runtime.start()
        try:
            runtime.publish(_candidate())
            for minute in range(1, 61):
                runtime.publish(_bar(minute, 100.0 + minute * 0.02))
            await asyncio.wait_for(runtime.publisher.queue.join(), timeout=5)
            record = runtime.evidence.get("c1", "harvest_v2", "v2_giveback_confirmed")
            assert record is not None
            assert record["decision"] in {"REPLAYED", "UNAVAILABLE"}
            assert runtime.telemetry()["harvest_v2"]["replays"] == 1
            assert _audit_rows(db, "shadow:harvest_v2")
        finally:
            await runtime.stop()
            db.close()
    asyncio.run(run())


def test_target_d_unavailable_reason_is_explicit(tmp_path):
    db = Database(tmp_path / "shadow.db")
    runtime = ShadowResearchRuntime(db=db, gemini_api_key=None)
    observation = runtime.target_d.consume(_candidate())
    assert observation is not None
    if observation["target_d_decision"] == "UNAVAILABLE":
        assert observation.get("unavailable_reason")
    db.close()
