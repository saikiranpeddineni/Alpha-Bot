from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.research.shadow_event import ShadowEventType, TradingObservationEvent
from app.research.shadow_event_publisher import ShadowEventPublisher


def _candidate(**overrides):
    payload = {
        "event_type": ShadowEventType.CANDIDATE,
        "event_id": "evt-1",
        "candidate_id": "cand-1",
        "ts": datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
        "decision_bar_timestamp_utc": datetime(2026, 7, 24, 14, 29, tzinfo=UTC),
        "symbol": "NVDA",
        "side": "long",
        "strategy": "momentum",
        "price": 100.0,
        "ml_probability": 0.61,
        "meta_probability": 0.58,
        "trade_score": 67.0,
        "champion_final_decision": "accepted",
    }
    payload.update(overrides)
    return TradingObservationEvent(**payload)


def test_candidate_requires_stable_candidate_id():
    with pytest.raises(ValidationError):
        _candidate(candidate_id=None)


def test_event_requires_timezone_aware_timestamp():
    with pytest.raises(ValidationError):
        _candidate(ts=datetime(2026, 7, 24, 14, 30))


def test_market_bar_cannot_invent_candidate_identity():
    with pytest.raises(ValidationError):
        TradingObservationEvent(
            event_type=ShadowEventType.MARKET_BAR,
            event_id="bar-1",
            candidate_id="fake-candidate",
            ts=datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
            symbol="NVDA",
            open=99.0,
            high=101.0,
            low=98.5,
            close=100.0,
        )


def test_missing_expectancy_stays_missing_in_spawn_adapter():
    payload = _candidate().legacy_spawn_payload()
    assert payload["calibrated_expected_net_dollars"] is None
    assert payload["expected_holding_mins"] is None
    assert payload["expectancy_confidence"] is None
    assert payload["atr"] is None
    assert payload["rsi"] is None


def test_calibrated_source_requires_calibrated_value():
    with pytest.raises(ValidationError):
        _candidate(expectancy_source="CALIBRATED", calibrated_expected_net_dollars=None)


def test_model_is_frozen():
    event = _candidate()
    with pytest.raises(ValidationError):
        event.price = 200.0


@pytest.mark.asyncio
async def test_queue_full_is_non_blocking_and_counted():
    publisher = ShadowEventPublisher(max_queue_size=1)
    publisher.running = True  # deliberately do not start worker so queue remains full
    assert publisher.publish(_candidate(event_id="evt-1", candidate_id="cand-1")) is True
    assert publisher.publish(_candidate(event_id="evt-2", candidate_id="cand-2")) is False
    assert publisher.events_dropped == 1


@pytest.mark.asyncio
async def test_consumer_failure_does_not_stop_other_consumers():
    publisher = ShadowEventPublisher(max_queue_size=4)
    received = []

    async def broken(_event):
        raise RuntimeError("research failure")

    async def healthy(event):
        received.append(event.candidate_id)

    publisher.register("broken", broken)
    publisher.register("healthy", healthy)
    await publisher.start()
    try:
        assert publisher.publish(_candidate()) is True
        await asyncio.wait_for(publisher.queue.join(), timeout=1.0)
        assert received == ["cand-1"]
        assert publisher.consumer_errors == 1
        assert publisher.events_processed == 1
    finally:
        await publisher.stop()


def test_shadow_boundary_has_no_broker_dependency():
    import app.research.shadow_event as event_module
    import app.research.shadow_event_publisher as publisher_module

    event_names = set(event_module.__dict__)
    publisher_names = set(publisher_module.__dict__)
    assert "Broker" not in event_names
    assert "make_broker" not in event_names
    assert "Broker" not in publisher_names
    assert "make_broker" not in publisher_names
