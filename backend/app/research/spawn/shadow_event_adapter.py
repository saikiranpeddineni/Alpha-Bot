"""Canonical shadow-event adapter for the existing Spawn coordinator."""
from __future__ import annotations

from app.research.shadow_event import ShadowEventType, TradingObservationEvent
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator


class SpawnShadowEventAdapter:
    """One-way adapter; intentionally exposes no execution result channel.

    The boolean return reports research-queue acceptance only. It is telemetry for the
    shadow runtime and has no path back into Champion decisions.
    """

    def __init__(self, coordinator: SpawnShadowCoordinator) -> None:
        self.coordinator = coordinator
        self.incomplete_market_bars = 0
        self.queue_rejections = 0

    def consume(self, event: TradingObservationEvent) -> bool:
        if event.event_type == ShadowEventType.MARKET_BAR:
            if None in (event.open, event.high, event.low, event.close):
                self.incomplete_market_bars += 1
                return False
            accepted = self.coordinator.publish_market_bar(
                event.symbol, float(event.open), float(event.high), float(event.low), float(event.close), event.ts,
            )
        else:
            accepted = self.coordinator.publish_observation(event.legacy_spawn_payload())
        if not accepted:
            self.queue_rejections += 1
        return accepted

    def telemetry(self) -> dict[str, int]:
        return {"incomplete_market_bars": self.incomplete_market_bars,
                "queue_rejections": self.queue_rejections}
