"""Composition root for all non-authoritative research consumers.

TradingEngine should know this runtime, not individual research implementations.
"""
from __future__ import annotations

from typing import Any

from app.ai.gemini_ranker import GeminiShadowRanker
from app.ai.shadow_bridge import GeminiShadowBridge
from app.ai.target_d_observer import TargetDShadowObserver
from app.ai.target_d_shadow_consumer import TargetDShadowConsumer
from app.db.database import Database
from app.research.shadow_event import TradingObservationEvent
from app.research.shadow_event_publisher import ShadowEventPublisher
from app.research.spawn.shadow_event_adapter import SpawnShadowEventAdapter
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator


class ShadowResearchRuntime:
    """Owns the one-way production-to-research event boundary."""

    def __init__(
        self,
        *,
        db: Database,
        gemini_api_key: str | None,
        gemini_observation_sink=None,
        target_d_observation_sink=None,
        max_queue_size: int = 2000,
    ) -> None:
        self.publisher = ShadowEventPublisher(max_queue_size=max_queue_size)
        self.spawn = SpawnShadowCoordinator(db=db)
        self.spawn_adapter = SpawnShadowEventAdapter(self.spawn)
        self.gemini = GeminiShadowBridge(
            GeminiShadowRanker(gemini_api_key or ""),
            observation_sink=gemini_observation_sink,
        )
        self.target_d = TargetDShadowConsumer(
            TargetDShadowObserver(),
            observation_sink=target_d_observation_sink,
        )
        self.publisher.register("spawn", self.spawn_adapter.consume)
        self.publisher.register("gemini", self.gemini.consume)
        self.publisher.register("target_d", self.target_d.consume)

    async def start(self) -> None:
        await self.spawn.start()
        await self.publisher.start()

    async def stop(self) -> None:
        # Stop ingress first, then cancel consumer-owned background work.
        await self.publisher.stop()
        await self.gemini.shutdown()
        await self.spawn.stop()

    def publish(self, event: TradingObservationEvent) -> bool:
        return self.publisher.publish(event)

    def telemetry(self) -> dict[str, Any]:
        return {
            "publisher": self.publisher.telemetry(),
            "spawn": self.spawn.telemetry(),
        }
