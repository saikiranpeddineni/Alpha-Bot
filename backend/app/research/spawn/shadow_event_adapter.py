"""Canonical shadow-event adapter for the existing Spawn coordinator."""
from __future__ import annotations

from app.research.shadow_event import ShadowEventType, TradingObservationEvent
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator


class SpawnShadowEventAdapter:
    """One-way adapter; intentionally exposes no execution result channel."""

    def __init__(self, coordinator: SpawnShadowCoordinator) -> None:
        self.coordinator = coordinator

    def consume(self, event: TradingObservationEvent) -> None:
        if event.event_type == ShadowEventType.MARKET_BAR:
            self.coordinator.publish_market_bar(
                event.symbol,
                float(event.open or event.close or 0.0),
                float(event.high or event.close or 0.0),
                float(event.low or event.close or 0.0),
                float(event.close or 0.0),
                event.ts,
            )
            return
        self.coordinator.publish_observation(event.legacy_spawn_payload())
