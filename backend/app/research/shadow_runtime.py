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
from app.research.evidence_store import UnifiedEvidenceStore
from app.research.exit_challenger_consumer import CanonicalExitChallengerConsumer
from app.research.outcome_joiner import CanonicalOutcomeJoiner
from app.research.shadow_event import TradingObservationEvent
from app.research.shadow_event_publisher import ShadowEventPublisher
from app.research.spawn.shadow_event_adapter import SpawnShadowEventAdapter
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator


class ShadowResearchRuntime:
    """Owns the one-way production-to-research event boundary."""

    def __init__(self, *, db: Database, gemini_api_key: str | None,
                 gemini_observation_sink=None, target_d_observation_sink=None,
                 max_queue_size: int = 2000) -> None:
        self.publisher = ShadowEventPublisher(max_queue_size=max_queue_size)
        self.evidence = UnifiedEvidenceStore(db)
        self.spawn = SpawnShadowCoordinator(db=db)
        self.spawn_adapter = SpawnShadowEventAdapter(self.spawn)
        self.outcomes = CanonicalOutcomeJoiner(self._record_outcome)
        self.exits = CanonicalExitChallengerConsumer(self._record_exit)

        def gemini_sink(observation: dict[str, Any]) -> None:
            if gemini_observation_sink is not None:
                gemini_observation_sink(observation)

        def target_d_sink(observation: dict[str, Any]) -> None:
            if target_d_observation_sink is not None:
                target_d_observation_sink(observation)

        self.gemini = GeminiShadowBridge(GeminiShadowRanker(gemini_api_key or ""), observation_sink=gemini_sink)
        self.target_d = TargetDShadowConsumer(TargetDShadowObserver(), observation_sink=target_d_sink)
        self.publisher.register("evidence_champion", self._record_champion)
        self.publisher.register("outcomes", self.outcomes.consume)
        self.publisher.register("exit_challengers", self.exits.consume)
        self.publisher.register("spawn", self.spawn_adapter.consume)
        self.publisher.register("gemini", self.gemini.consume)
        self.publisher.register("target_d", self._consume_target_d)

    def _record_champion(self, event: TradingObservationEvent) -> None:
        if event.candidate_id:
            self.evidence.record(event, challenger_id="champion", challenger_version="production",
                                 decision=event.champion_final_decision)

    def _record_outcome(self, event: TradingObservationEvent, horizon: str, outcome: dict[str, Any]) -> None:
        if event.candidate_id:
            self.evidence.record(event, challenger_id="outcome", challenger_version=horizon,
                                 decision="STOPPED" if outcome.get("stopped") else "MARK_TO_MARKET", evidence=outcome)

    def _record_exit(self, event: TradingObservationEvent, policy: str, evidence: dict[str, Any]) -> None:
        if event.candidate_id:
            self.evidence.record(event, challenger_id="exit_policy", challenger_version=policy,
                                 decision="AMBIGUOUS" if evidence.get("ambiguous") else "REPLAYED", evidence=evidence)

    def _consume_target_d(self, event: TradingObservationEvent) -> None:
        observation = self.target_d.consume(event)
        if observation is not None and event.candidate_id:
            self.evidence.record(event, challenger_id="target_d", challenger_version="v1",
                                 decision=observation.get("target_d_decision"), evidence=observation)

    async def start(self) -> None:
        await self.spawn.start()
        await self.publisher.start()

    async def stop(self) -> None:
        await self.publisher.stop()
        await self.gemini.shutdown()
        await self.spawn.stop()

    def publish(self, event: TradingObservationEvent) -> bool:
        return self.publisher.publish(event)

    def telemetry(self) -> dict[str, Any]:
        return {"publisher": self.publisher.telemetry(), "spawn": self.spawn.telemetry(),
                "evidence": self.evidence.telemetry(), "outcomes": self.outcomes.telemetry(),
                "exit_challengers": self.exits.telemetry()}
