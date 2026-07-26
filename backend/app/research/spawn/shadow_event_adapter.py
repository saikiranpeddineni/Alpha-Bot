"""Canonical shadow-event adapter for the existing Spawn coordinator."""
from __future__ import annotations

from app.research.shadow_event import TradingObservationEvent
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator


class SpawnShadowEventAdapter:
    """One-way adapter; intentionally exposes no execution result channel."""

    def __init__(self, coordinator: SpawnShadowCoordinator) -> None:
        self.coordinator = coordinator

    def consume(self, event: TradingObservationEvent) -> None:
        self.coordinator.publish_observation(event.legacy_spawn_payload())
