"""Canonical-event consumer for Target D passive challenger."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.ai.target_d_observer import TargetDShadowObserver
from app.core.logging import applog
from app.research.shadow_event import ShadowEventType, TradingObservationEvent

TargetDSink = Callable[[dict[str, Any]], None]


class TargetDShadowConsumer:
    """Passive Target D evaluator with no production decision result channel."""

    def __init__(self, observer: TargetDShadowObserver, *, observation_sink: TargetDSink | None = None) -> None:
        self.observer = observer
        self.observation_sink = observation_sink

    def consume(self, event: TradingObservationEvent) -> dict[str, Any] | None:
        if event.event_type != ShadowEventType.CANDIDATE:
            return None
        result = self.observer.evaluate_passive(
            dict(event.features) if event.features else None,
            event.ml_probability,
            event.ml_decision or "UNAVAILABLE",
            event.meta_decision,
        )
        observation = {
            "candidate_id": event.candidate_id,
            "event_id": event.event_id,
            "symbol": event.symbol,
            "strategy": event.strategy,
            "ts": event.ts.isoformat(),
            **result,
        }
        if self.observation_sink is not None:
            try:
                self.observation_sink(observation)
            except Exception as exc:
                applog.warning("target_d_shadow", f"Target D observation sink failed open: {exc}")
        return observation
