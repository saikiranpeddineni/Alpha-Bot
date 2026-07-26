from datetime import UTC, datetime

from app.research.shadow_event import ShadowEventType, TradingObservationEvent
from app.research.spawn.shadow_event_adapter import SpawnShadowEventAdapter


class Coordinator:
    def __init__(self):
        self.bars = []
        self.observations = []

    def publish_market_bar(self, *args):
        self.bars.append(args)

    def publish_observation(self, payload):
        self.observations.append(payload)


def market_bar(**overrides):
    values = dict(
        event_type=ShadowEventType.MARKET_BAR,
        event_id="bar-1",
        ts=datetime(2026, 7, 24, 14, 31, tzinfo=UTC),
        symbol="NVDA",
        open=100.0,
        high=101.0,
        low=99.5,
        close=100.5,
    )
    values.update(overrides)
    return TradingObservationEvent(**values)


def test_complete_market_bar_is_forwarded_exactly():
    coordinator = Coordinator()
    adapter = SpawnShadowEventAdapter(coordinator)
    adapter.consume(market_bar())
    assert coordinator.bars == [("NVDA", 100.0, 101.0, 99.5, 100.5,
                                 datetime(2026, 7, 24, 14, 31, tzinfo=UTC))]
    assert adapter.telemetry()["incomplete_market_bars"] == 0


def test_incomplete_market_bar_is_dropped_not_zero_filled():
    coordinator = Coordinator()
    adapter = SpawnShadowEventAdapter(coordinator)
    adapter.consume(market_bar(low=None))
    assert coordinator.bars == []
    assert adapter.telemetry()["incomplete_market_bars"] == 1
