"""Canonical shadow-event adapter for the existing Spawn coordinator."""
from __future__ import annotations

from app.research.shadow_event import ShadowEventType, TradingObservationEvent
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator


class SpawnShadowEventAdapter:
    """One-way adapter; intentionally exposes no execution result channel.

    Incomplete market observations are dropped rather than converted to zero-valued
    OHLC bars. Research evidence must describe the actual production observation or
    remain unavailable; it must never manufacture prices.
    """

    def __init__(self, coordinator: SpawnShadowCoordinator) -> None:
        self.coordinator = coordinator
        self.incomplete_market_bars = 0

    def consume(self, event: TradingObservationEvent) -> None:
        if event.event_type == ShadowEventType.MARKET_BAR:
            if None in (event.open, event.high, event.low, event.close):
                self.incomplete_market_bars += 1
                return
            self.coordinator.publish_market_bar(
                event.symbol,
                float(event.open),
                float(event.high),
                float(event.low),
                float(event.close),
                event.ts,
            )
            return
        self.coordinator.publish_observation(event.legacy_spawn_payload())

    def telemetry(self) -> dict[str, int]:
        return {"incomplete_market_bars": self.incomplete_market_bars}
