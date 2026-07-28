"""Composition root for the complete non-authoritative ShadowAlphaBot research plane."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.ai.gemini_ranker import GeminiShadowRanker
from app.ai.shadow_bridge import GeminiShadowBridge
from app.ai.target_d_observer import TargetDShadowObserver
from app.ai.target_d_shadow_consumer import TargetDShadowConsumer
from app.core.logging import applog
from app.db.database import Database
from app.research.evidence_store import UnifiedEvidenceStore
from app.research.exit_challenger_consumer import CanonicalExitChallengerConsumer
from app.research.module_audit_store import ShadowModuleAuditStore
from app.research.outcome_joiner import CanonicalOutcomeJoiner
from app.research.shadow_event import ShadowEventType, TradingObservationEvent
from app.research.shadow_event_publisher import ShadowEventPublisher
from app.research.spawn.shadow_event_adapter import SpawnShadowEventAdapter
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator


class ShadowResearchRuntime:
    """One research plane, one evidence contract, one hard production firewall."""

    def __init__(self, *, db: Database, gemini_api_key: str | None,
                 gemini_observation_sink=None, target_d_observation_sink=None,
                 max_queue_size: int = 2000) -> None:
        self.publisher = ShadowEventPublisher(max_queue_size=max_queue_size)
        self.evidence = UnifiedEvidenceStore(db, persist_every=50)
        self.audit = ShadowModuleAuditStore(db)
        self.spawn = SpawnShadowCoordinator(db=db)
        self.spawn_adapter = SpawnShadowEventAdapter(self.spawn)
        self.outcomes = CanonicalOutcomeJoiner(self._record_outcome)
        self.exits = CanonicalExitChallengerConsumer(self._record_exit)
        self._started = False
        self._lifecycle_errors = 0
        self._module_events: dict[str, int] = {
            "event_ingress": 0,
            "champion": 0,
            "discovery": 0,
            "discovery_validation": 0,
            "outcomes": 0,
            "legacy_shadow_ledger": 0,
            "exit_challengers": 0,
            "harvest_v2": 0,
            "spawn": 0,
            "gemini": 0,
            "target_d": 0,
        }

        def gemini_sink(observation: dict[str, Any]) -> None:
            identity = f"{observation.get('session')}:{observation.get('candidate_fingerprint')}"
            self._module_events["gemini"] += 1
            self.audit.record("gemini", observation, identity=identity)
            if gemini_observation_sink is not None:
                gemini_observation_sink(observation)

        def target_d_sink(observation: dict[str, Any]) -> None:
            identity = f"{observation.get('candidate_id')}:{observation.get('event_id')}"
            self._module_events["target_d"] += 1
            self.audit.record("target_d", observation, identity=identity)
            if target_d_observation_sink is not None:
                target_d_observation_sink(observation)

        self.gemini = GeminiShadowBridge(GeminiShadowRanker(gemini_api_key or ""), observation_sink=gemini_sink)
        self.target_d = TargetDShadowConsumer(TargetDShadowObserver(), observation_sink=target_d_sink)
        self.publisher.register("shadow_ingress", self._record_ingress)
        self.publisher.register("evidence_champion", self._record_champion)
        self.publisher.register("discovery", self._record_discovery)
        self.publisher.register("outcomes", self.outcomes.consume)
        self.publisher.register("legacy_shadow_ledger_bridge", self._record_legacy_shadow_reachability)
        self.publisher.register("exit_challengers", self.exits.consume)
        self.publisher.register("spawn", self._consume_spawn)
        self.publisher.register("gemini", self.gemini.consume)
        self.publisher.register("target_d", self._consume_target_d)

    def _record_ingress(self, event: TradingObservationEvent) -> None:
        self._module_events["event_ingress"] += 1
        self.audit.record("event_ingress", {"event": event.model_dump(mode="json")}, identity=event.event_id, ts=event.ts)

    def _record_champion(self, event: TradingObservationEvent) -> None:
        if not event.candidate_id:
            return
        self._module_events["champion"] += 1
        self.evidence.record(event, challenger_id="champion", challenger_version="production",
                             decision=event.champion_final_decision)
        self.audit.record("champion", {
            "candidate_id": event.candidate_id, "event_id": event.event_id,
            "decision": event.champion_final_decision, "rejection_reason": event.rejection_reason,
            "ml_decision": event.ml_decision, "meta_decision": event.meta_decision,
            "trade_score_decision": event.trade_score_decision,
            "portfolio_decision": event.portfolio_decision, "risk_decision": event.risk_decision,
        }, identity=event.event_id, ts=event.ts)

    def _record_discovery(self, event: TradingObservationEvent) -> None:
        if event.event_type != ShadowEventType.CANDIDATE or not event.candidate_id:
            return
        self._module_events["discovery"] += 1
        evidence = {"discovery_source": event.discovery_source, "scanner_score": event.scanner_score,
                    "scanner_auc": event.scanner_auc, "symbol": event.symbol}
        self.evidence.record(event, challenger_id="discovery", challenger_version="observed_source_v1",
                             decision=event.discovery_source or "UNAVAILABLE", evidence=evidence)
        self.audit.record("discovery", evidence, identity=event.candidate_id, ts=event.ts)

    def record_discovery_session(self, session: str, evidence: dict[str, Any]) -> bool:
        """Persist fixed-vs-dynamic discovery evidence through the common DB audit plane.

        The offline JSONL validator remains a pure analysis codec. Runtime callers use this
        method so discovery evidence is no longer an unobservable side island.
        """
        self._module_events["discovery_validation"] += 1
        return self.audit.record("discovery_session", {"session": session, **evidence}, identity=session)

    def _record_legacy_shadow_reachability(self, event: TradingObservationEvent) -> None:
        """Bridge legacy ShadowLedger semantics into the canonical audit plane.

        CanonicalOutcomeJoiner is the authoritative forward-outcome calculator. We do not
        recompute outcomes here because the legacy dataframe look-ahead path can disagree on
        gap-through-stop fills. This bridge makes the legacy surface observable/reachable while
        keeping one causal outcome truth and zero production authority.
        """
        if event.event_type != ShadowEventType.CANDIDATE or not event.candidate_id:
            return
        self._module_events["legacy_shadow_ledger"] += 1
        self.audit.record("legacy_shadow_ledger", {
            "candidate_id": event.candidate_id,
            "event_id": event.event_id,
            "status": "CANONICAL_OUTCOME_JOINER_AUTHORITATIVE",
            "legacy_forward_outcome_recompute": False,
            "reason": "avoid duplicate/non-causal dataframe replay and gap-through-stop disagreement",
        }, identity=event.candidate_id, ts=event.ts)

    def _record_outcome(self, event: TradingObservationEvent, horizon: str, outcome: dict[str, Any]) -> None:
        if not event.candidate_id:
            return
        self._module_events["outcomes"] += 1
        decision = "STOPPED" if outcome.get("stopped") else "MARK_TO_MARKET"
        self.evidence.record(event, challenger_id="outcome", challenger_version=horizon,
                             decision=decision, evidence=outcome)
        self.audit.record("outcome", outcome, identity=f"{event.candidate_id}:{horizon}", ts=event.ts)

    def _record_exit(self, event: TradingObservationEvent, policy: str, evidence: dict[str, Any]) -> None:
        if not event.candidate_id:
            return
        self._module_events["exit_challengers"] += 1
        decision = "UNAVAILABLE" if evidence.get("replay_valid") is False else (
            "AMBIGUOUS" if evidence.get("ambiguous") else "REPLAYED")
        is_harvest = policy == "harvest_v2_giveback_confirmed"
        challenger_id = "harvest_v2" if is_harvest else "exit_policy"
        version = str(evidence.get("policy_version") or policy) if is_harvest else policy
        if is_harvest:
            self._module_events["harvest_v2"] += 1
        self.evidence.record(event, challenger_id=challenger_id, challenger_version=version,
                             decision=decision, evidence=evidence)
        self.audit.record("harvest_v2" if is_harvest else "exit_policy", {"policy": policy, **evidence},
                          identity=f"{event.candidate_id}:{policy}", ts=event.ts)

    def _consume_spawn(self, event: TradingObservationEvent) -> None:
        queued = self.spawn_adapter.consume(event)
        self._module_events["spawn"] += 1
        self.audit.record("spawn", {"event_id": event.event_id, "candidate_id": event.candidate_id,
                                    "queued": queued, "telemetry": self.spawn.telemetry()},
                          identity=event.event_id, ts=event.ts)

    def _consume_target_d(self, event: TradingObservationEvent) -> None:
        observation = self.target_d.consume(event)
        if observation is not None and event.candidate_id:
            self.evidence.record(event, challenger_id="target_d", challenger_version="v1",
                                 decision=observation.get("target_d_decision"), evidence=observation)

    def record_external_module(self, module: str, payload: dict[str, Any], *, identity: str | None = None,
                               ts: datetime | None = None) -> bool:
        """Bridge legacy/replay/rotation producers into the common DB audit."""
        self._module_events[module] = self._module_events.get(module, 0) + 1
        return self.audit.record(module, payload, identity=identity, ts=ts)

    async def start(self) -> None:
        if self._started:
            return
        try:
            await self.publisher.start()
        except Exception as exc:
            self._lifecycle_errors += 1
            applog.error("shadow_research", f"publisher startup failed (fail-open): {exc}")
        try:
            await self.spawn.start()
        except Exception as exc:
            self._lifecycle_errors += 1
            applog.error("shadow_research", f"spawn startup failed (fail-open): {exc}")
        self._started = True
        self.audit.record("runtime", {"event": "START", "telemetry": self.telemetry()})

    async def stop(self) -> None:
        self.audit.record("runtime", {"event": "STOP", "telemetry": self.telemetry()})
        self.evidence.flush()
        for name, cleanup in (("publisher", self.publisher.stop), ("gemini", self.gemini.shutdown), ("spawn", self.spawn.stop)):
            try:
                await cleanup()
            except Exception as exc:
                self._lifecycle_errors += 1
                applog.error("shadow_research", f"{name} shutdown failed (fail-open): {exc}")
        self._started = False

    def publish(self, event: TradingObservationEvent) -> bool:
        try:
            return self.publisher.publish(event)
        except Exception as exc:
            self._lifecycle_errors += 1
            applog.error("shadow_research", f"event publish failed (fail-open): {exc}")
            return False

    def telemetry(self) -> dict[str, Any]:
        return {
            "publisher": self.publisher.telemetry(), "spawn": self.spawn.telemetry(),
            "spawn_adapter": self.spawn_adapter.telemetry(), "audit": self.audit.telemetry(),
            "evidence": self.evidence.telemetry(), "outcomes": self.outcomes.telemetry(),
            "exit_challengers": self.exits.telemetry(), "module_events": dict(self._module_events),
            "target_d": {"enabled": True, "generation_id": self.target_d.observer.generation_id,
                         "model_available": self.target_d.observer.model is not None,
                         "unavailable_reason": self.target_d.observer.unavailable_reason},
            "gemini": self.gemini.ranker.telemetry(), "started": self._started,
            "lifecycle_errors": self._lifecycle_errors,
        }
