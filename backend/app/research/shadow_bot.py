"""Shadow AlphaBot control plane built only from existing research components.

This is deliberately NOT a second production TradingEngine. It owns no broker,
order submission, production risk, model training, or promotion authority.
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
    gemini_ranker: bool = True
    spawn: bool = True
    spawn_rotation: bool = True
    outcome_joiner: bool = True
    rejected_signal_replay_evidence: bool = True
    discovery_evidence: bool = True
    exit_challengers: bool = True
    harvest_v2: bool = True
    durable_module_audit: bool = True


class ShadowAlphaBotRuntime:
    """Single lifecycle, publish, telemetry, and audit facade for the shadow stack."""

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
        return self.research.publish(event)

    def record_external_module(self, module: str, payload: dict[str, Any], *, identity: str | None = None,
                               ts: datetime | None = None) -> bool:
        return self.research.record_external_module(module, payload, identity=identity, ts=ts)

    def record_discovery_session(self, session: str, evidence: dict[str, Any]) -> bool:
        return self.research.record_discovery_session(session, evidence)

    def telemetry(self) -> dict[str, Any]:
        research = self.research.telemetry()
        publisher = research.get("publisher", {})
        spawn = research.get("spawn", {})
        exits = research.get("exit_challengers", {})
        harvest = {"replays": exits.get("harvest_replays", 0), "shared_path": True}
        return {
            "name": "ShadowAlphaBot",
            "started": bool(research.get("started")),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "capabilities": self.capabilities.__dict__.copy(),
            "health": {
                "queue_depth": publisher.get("queue_size", 0), "lag_seconds": publisher.get("lag_seconds"),
                "events_published": publisher.get("events_published", 0),
                "events_processed": publisher.get("events_processed", 0),
                "events_dropped": publisher.get("events_dropped", 0),
                "events_duplicate": publisher.get("events_duplicate", 0),
                "consumer_errors": publisher.get("consumer_errors", 0),
            },
            "coverage": {
                "spawn_observations": spawn.get("observations_processed", 0),
                "spawn_candidates": spawn.get("candidate_observations_processed", 0),
                "spawn_market_bars": spawn.get("market_bars_processed", 0),
                "evidence_records": research.get("evidence", {}).get("records", 0),
                "audit_records": research.get("audit", {}).get("records_written", 0),
                "outcomes_emitted": research.get("outcomes", {}).get("outcomes_emitted", 0),
                "exit_replays": exits.get("replays", 0),
                "harvest_v2_replays": exits.get("harvest_replays", 0),
            },
            "target_d": research.get("target_d", {}), "gemini": research.get("gemini", {}),
            "spawn": spawn, "discovery": {"persisted": True}, "harvest_v2": harvest,
            "audit": research.get("audit", {}), "evidence": research.get("evidence", {}),
            "outcomes": research.get("outcomes", {}), "exit_challengers": exits,
            "lifecycle_errors": research.get("lifecycle_errors", 0),
        }
