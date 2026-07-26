"""Single non-blocking fan-out boundary from production observations to research."""
from __future__ import annotations

import asyncio
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
    """Bounded, fail-open event fan-out with no result channel to Champion.

    ``publish`` is synchronous and uses ``put_nowait``. Slow/broken research can
    drop observations or record errors, but can never block a production tick.
    """

    def __init__(self, *, max_queue_size: int = 2000) -> None:
        self.queue: asyncio.Queue[TradingObservationEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._consumers: list[ShadowConsumerRegistration] = []
        self._worker_task: asyncio.Task | None = None
        self.running = False
        self.events_published = 0
        self.events_processed = 0
        self.events_dropped = 0
        self.consumer_errors = 0
        self.last_processed_at: datetime | None = None

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
        self.running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def publish(self, event: TradingObservationEvent) -> bool:
        if not self.running:
            return False
        try:
            self.queue.put_nowait(event)
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
                            applog.warning(
                                "shadow_event_publisher",
                                f"Consumer {registration.name} failed open: {exc}",
                            )
                    self.events_processed += 1
                    self.last_processed_at = datetime.now(timezone.utc)
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.consumer_errors += 1
                applog.warning("shadow_event_publisher", f"Worker failed open: {exc}")

    def telemetry(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "queue_size": self.queue.qsize(),
            "events_published": self.events_published,
            "events_processed": self.events_processed,
            "events_dropped": self.events_dropped,
            "consumer_errors": self.consumer_errors,
            "consumer_count": len(self._consumers),
            "last_processed_at": self.last_processed_at.isoformat() if self.last_processed_at else None,
        }
