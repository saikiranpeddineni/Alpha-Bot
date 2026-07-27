"""Single non-blocking fan-out boundary from production observations to research."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.logging import applog
from app.research.shadow_event import TradingObservationEvent

ShadowConsumer = Callable[[TradingObservationEvent], Awaitable[None] | None]


@dataclass(frozen=True)
class ShadowConsumerRegistration:
    name: str
    consumer: ShadowConsumer


class ShadowEventPublisher:
    """Bounded fail-open fan-out with no result channel to Champion."""

    def __init__(self, *, max_queue_size: int = 2000, dedupe_window: int = 10000) -> None:
        self.queue: asyncio.Queue[TradingObservationEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._consumers: list[ShadowConsumerRegistration] = []
        self._worker_task: asyncio.Task | None = None
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._dedupe_window = max(1, dedupe_window)
        self.running = False
        self.events_published = 0
        self.events_processed = 0
        self.events_dropped = 0
        self.events_duplicate = 0
        self.consumer_errors = 0
        self.shutdown_undrained = 0
        self.last_processed_at: datetime | None = None
        self.last_event_timestamp: datetime | None = None

    def register(self, name: str, consumer: ShadowConsumer) -> None:
        if self.running:
            raise RuntimeError("shadow consumers must be registered before publisher start")
        if any(item.name == name for item in self._consumers):
            raise ValueError(f"duplicate shadow consumer: {name}")
        self._consumers.append(ShadowConsumerRegistration(name=name, consumer=consumer))

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._worker_task = asyncio.create_task(self._worker(), name="shadow-event-publisher")

    async def stop(self) -> None:
        if self._worker_task is not None and self.running:
            # Preserve queued evidence on normal shutdown, but never let research hold
            # production teardown indefinitely.
            try:
                await asyncio.wait_for(self.queue.join(), timeout=2.0)
            except asyncio.TimeoutError:
                self.shutdown_undrained += self.queue.qsize()
                applog.warning("shadow_event_publisher", f"Shutdown with {self.queue.qsize()} undrained shadow events")
        self.running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def _remember(self, event_id: str) -> None:
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        while len(self._seen_order) > self._dedupe_window:
            self._seen_ids.discard(self._seen_order.popleft())

    def publish(self, event: TradingObservationEvent) -> bool:
        if not self.running:
            return False
        if event.event_id in self._seen_ids:
            self.events_duplicate += 1
            return True
        try:
            self.queue.put_nowait(event)
            self._remember(event.event_id)
            self.events_published += 1
            return True
        except asyncio.QueueFull:
            self.events_dropped += 1
            return False
        except Exception as exc:
            self.events_dropped += 1
            applog.warning("shadow_event_publisher", f"Shadow publish failed open: {exc}")
            return False

    async def _worker(self) -> None:
        while self.running:
            try:
                event = await self.queue.get()
                try:
                    for registration in tuple(self._consumers):
                        try:
                            result = registration.consumer(event)
                            if result is not None:
                                await result
                        except Exception as exc:
                            self.consumer_errors += 1
                            applog.warning("shadow_event_publisher", f"Consumer {registration.name} failed open: {exc}")
                    self.events_processed += 1
                    self.last_event_timestamp = event.ts
                    self.last_processed_at = datetime.now(timezone.utc)
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.consumer_errors += 1
                applog.warning("shadow_event_publisher", f"Worker failed open: {exc}")

    def telemetry(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        lag_seconds = None
        if self.last_event_timestamp is not None:
            lag_seconds = max(0.0, (now - self.last_event_timestamp).total_seconds())
        return {"running": self.running, "queue_size": self.queue.qsize(),
                "queue_capacity": self.queue.maxsize, "events_published": self.events_published,
                "events_processed": self.events_processed, "events_dropped": self.events_dropped,
                "events_duplicate": self.events_duplicate, "consumer_errors": self.consumer_errors,
                "shutdown_undrained": self.shutdown_undrained, "consumer_count": len(self._consumers),
                "lag_seconds": lag_seconds,
                "last_processed_at": self.last_processed_at.isoformat() if self.last_processed_at else None,
                "last_event_timestamp": self.last_event_timestamp.isoformat() if self.last_event_timestamp else None}
