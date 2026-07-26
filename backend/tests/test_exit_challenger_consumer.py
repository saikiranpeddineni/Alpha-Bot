from datetime import UTC, datetime, timedelta

from app.research.exit_challenger_consumer import CanonicalExitChallengerConsumer
from app.research.shadow_event import ShadowEventType, TradingObservationEvent

T0 = datetime(2026, 7, 24, 14, 30, tzinfo=UTC)


def candidate(*, atr=1.0):
    return TradingObservationEvent(event_type=ShadowEventType.CANDIDATE, event_id="c", candidate_id="cand-1",
        ts=T0, decision_bar_timestamp_utc=T0-timedelta(minutes=1), symbol="NVDA", side="long",
        strategy="momentum", price=100.0, stop_loss=99.0, atr=atr, champion_final_decision="accepted")


def bar(minute):
    return TradingObservationEvent(event_type=ShadowEventType.MARKET_BAR, event_id=f"b{minute}",
        ts=T0+timedelta(minutes=minute), symbol="NVDA", open=100.0, high=100.5, low=99.5, close=100.2, atr=1.0)


def test_exit_policies_emit_under_same_candidate_after_60m():
    emitted=[]
    consumer=CanonicalExitChallengerConsumer(lambda event, policy, evidence: emitted.append((event.candidate_id, policy, evidence)))
    consumer.consume(candidate())
    consumer.consume(bar(30))
    assert emitted == []
    consumer.consume(bar(60))
    assert emitted
    assert all(item[0] == "cand-1" for item in emitted)
    assert {item[1] for item in emitted} >= {"fixed_1_2", "trail_2_atr", "hold_to_close"}


def test_future_replay_is_not_possible_without_future_bar():
    emitted=[]
    consumer=CanonicalExitChallengerConsumer(lambda *args: emitted.append(args))
    consumer.consume(candidate())
    consumer.consume(bar(59))
    assert emitted == []
    assert consumer.telemetry()["replays"] == 0


def test_missing_atr_is_unavailable_not_fabricated():
    emitted=[]
    consumer=CanonicalExitChallengerConsumer(lambda *args: emitted.append(args))
    consumer.consume(candidate(atr=None))
    consumer.consume(bar(60))
    assert emitted == []
    assert consumer.telemetry()["skipped_missing_risk"] == 1


def test_out_of_order_bar_is_ignored():
    emitted=[]
    consumer=CanonicalExitChallengerConsumer(lambda *args: emitted.append(args))
    consumer.consume(candidate())
    consumer.consume(TradingObservationEvent(event_type=ShadowEventType.MARKET_BAR, event_id="old", ts=T0,
        symbol="NVDA", open=100.0, high=200.0, low=50.0, close=150.0, atr=1.0))
    assert emitted == []
    assert consumer.telemetry()["out_of_order_bars"] == 1
