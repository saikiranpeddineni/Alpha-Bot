"""Shadow AlphaBot control plane built only from existing research components.

This is deliberately NOT a second production TradingEngine.  It owns no broker,
order submission, production risk, model training, or promotion authority.  It is
an orchestration/read-model facade over the existing ShadowResearchRuntime:
Target-D, Spawn, outcome joiner, and exit challengers (including Harvest V2
observations emitted by the Champion engine).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.research.shadow_event import TradingObservationEvent
from app.research.shadow_runtime import ShadowResearchRuntime


@dataclass(frozen=True)
class ShadowBotCapabilities:
    broker_authority: bool = False
    production_mutation: bool = False
    model_training: bool = False
    automatic_promotion: bool = False
    target_d: bool = True
    spawn: bool = True
    outcome_joiner: bool = True
    exit_challengers: bool = True


class ShadowAlphaBotRuntime:
    """Single lifecycle/telemetry boundary for the existing shadow stack."""

    def __init__(self, research: ShadowResearchRuntime):
        self.research = research
        self.capabilities = ShadowBotCapabilities()
        self.started_at: datetime | None = None
        self.stopped_at: datetime | None = None

    async def start(self) -> None:
        await self.research.start()
        self.started_at = datetime.now(UTC)
        self.stopped_at = None

    async def stop(self) -> None:
        await self.research.stop()
        self.stopped_at = datetime.now(UTC)

    def publish(self, event: TradingObservationEvent) -> bool:
        """One-way PROD -> SHADOW observation mirror."""
        return self.research.publish(event)

    def telemetry(self) -> dict[str, Any]:
        research = self.research.telemetry()
        publisher = research.get("publisher", {})
        spawn = research.get("spawn", {})
        target_d = research.get("target_d", {})
        return {
            "name": "ShadowAlphaBot",
            "started": bool(research.get("started")),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "capabilities": self.capabilities.__dict__.copy(),
            "health": {
                "queue_depth": publisher.get("queue_depth", 0),
                "queue_capacity": publisher.get("queue_capacity", 0),
                "lag_seconds": publisher.get("lag_seconds"),
                "events_published": publisher.get("events_published", 0),
                "events_processed": publisher.get("events_processed", 0),
                "events_dropped": publisher.get("events_dropped", 0),
                "events_duplicate": publisher.get("events_duplicate", 0),
                "consumer_errors": publisher.get("consumer_errors", 0),
                "consumer_errors_by_name": publisher.get("consumer_errors_by_name", {}),
            },
            "coverage": {
                "spawn_observations": spawn.get("observations_processed", 0),
                "spawn_candidates": spawn.get("candidate_observations_processed", 0),
                "spawn_market_bars": spawn.get("market_bars_processed", 0),
                "spawn_last_observation_ts": spawn.get("last_observation_ts"),
                "spawn_last_candidate_ts": spawn.get("last_candidate_ts"),
                "spawn_last_market_bar_ts": spawn.get("last_market_bar_ts"),
            },
            "target_d": target_d,
            "spawn": spawn,
            "outcomes": research.get("outcomes", {}),
            "exit_challengers": research.get("exit_challengers", {}),
            "lifecycle_errors": research.get("lifecycle_errors", 0),
        }
