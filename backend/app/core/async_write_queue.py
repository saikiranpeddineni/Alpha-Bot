# =============================================================================
# [CREATED 2026-11-11]: Asynchronous Non-Blocking Write Queue with TTL Drop Guard
# ORIGINAL INTENT: Unbounded standard queue.Queue instances allowed backlog growth
# during SQLite lock contention (OperationalError: database is locked), causing
# thread starvation and engine tick latency jitter.
# REASON FOR CHANGE: Implement bounded buffer (maxsize=5000), monotonic timestamping,
# non-blocking enqueue, and a 30-second TTL drop guard to eliminate lock contention
# pressure and protect engine execution throughput (R5).
# SAFE ROLLBACK: This is a new module providing AsyncWriteQueue and QueueItem.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import queue
import threading
import time
from typing import Any

applog = logging.getLogger("app")


@dataclass
class QueueItem:
    """Telemetry / shadow ledger queue item wrapped with monotonic enqueue timestamp."""

    payload: Any
    enqueued_at: float = field(default_factory=time.monotonic)

    def is_expired(self, ttl_seconds: float = 30.0, now: float | None = None) -> bool:
        """Return True if the item has lingered in queue longer than ttl_seconds."""
        current = time.monotonic() if now is None else now
        return (current - self.enqueued_at) > ttl_seconds

    def age(self, now: float | None = None) -> float:
        """Return elapsed seconds since the item was enqueued."""
        current = time.monotonic() if now is None else now
        return max(0.0, current - self.enqueued_at)

    def __getitem__(self, key: str) -> Any:
        """Forward dictionary subscripting to payload for transparent access."""
        if isinstance(self.payload, dict):
            return self.payload[key]
        raise TypeError(f"Payload of type {type(self.payload).__name__} is not subscriptable")

    def __contains__(self, key: str) -> bool:
        """Forward membership check to payload."""
        if isinstance(self.payload, dict):
            return key in self.payload
        return False

    def get(self, key: str, default: Any = None) -> Any:
        """Forward dictionary get() to payload."""
        if isinstance(self.payload, dict):
            return self.payload.get(key, default)
        return default


class AsyncWriteQueue(queue.Queue):
    """Bounded, thread-safe asynchronous write queue with 30-second TTL drop guard.

    Features:
    - Bounded buffer (default maxsize=5000) prevents memory bloat.
    - Strictly non-blocking enqueue (put_nowait) drops incoming items under capacity limits.
    - Automatic TTL filtering (default ttl_seconds=30.0) purges stale events on dequeue.
    - Lock contention handler discards stale in-flight batches during SQLite contention.
    - Exposes operational metrics (queue_depth, total_enqueued, total_written,
      dropped_ttl_count, dropped_capacity_count, lock_contention_events).
    """

    def __init__(self, maxsize: int = 5000, ttl_seconds: float = 30.0) -> None:
        super().__init__(maxsize=maxsize)
        self.ttl_seconds = float(ttl_seconds)
        self._total_enqueued = 0
        self._total_written = 0
        self._dropped_ttl_count = 0
        self._dropped_capacity_count = 0
        self._lock_contention_events = 0
        self._metrics_lock = threading.Lock()

    def put_nowait(self, item: Any) -> bool:  # type: ignore[override]
        """Non-blocking enqueue with capacity drop guard.

        Wraps item in QueueItem if not already wrapped. If queue is full,
        increments dropped_capacity_count and returns False rather than raising queue.Full,
        guaranteeing zero caller exception disruption.
        """
        wrapped = item if isinstance(item, QueueItem) else QueueItem(payload=item)
        with self.mutex:
            if self.maxsize > 0 and self._qsize() >= self.maxsize:
                with self._metrics_lock:
                    self._dropped_capacity_count += 1
                return False
            self._put(wrapped)
            self.unfinished_tasks += 1
            with self._metrics_lock:
                self._total_enqueued += 1
            self.not_empty.notify()
            return True

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        """Put item into queue. If non-blocking, delegates to put_nowait."""
        if not block:
            self.put_nowait(item)
            return
        wrapped = item if isinstance(item, QueueItem) else QueueItem(payload=item)
        super().put(wrapped, block=block, timeout=timeout)
        with self._metrics_lock:
            self._total_enqueued += 1

    def enqueue(self, payload: Any) -> bool:
        """Convenience alias for non-blocking enqueue."""
        return self.put_nowait(payload)

    def get(self, block: bool = True, timeout: float | None = None) -> QueueItem:  # type: ignore[override]
        """Retrieve the next unexpired item.

        Automatically purges and counts any queued items exceeding ttl_seconds,
        calling super().task_done() on dropped items so join()/flush() does not stall.
        """
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty

            raw_item = super().get(block=block, timeout=remaining)
            wrapped = raw_item if isinstance(raw_item, QueueItem) else QueueItem(payload=raw_item)

            if wrapped.is_expired(self.ttl_seconds):
                with self._metrics_lock:
                    self._dropped_ttl_count += 1
                super().task_done()
                if not block:
                    # In non-blocking mode, if this item expired, check next immediately
                    continue
                continue
            return wrapped

    def get_nowait(self) -> QueueItem:  # type: ignore[override]
        """Non-blocking retrieve of next unexpired item."""
        return self.get(block=False)

    def drain_batch(self, max_items: int = 200, timeout: float = 2.0) -> list[QueueItem]:
        """Drain a batch of up to max_items valid unexpired QueueItems.

        Blocks up to timeout on the first item, then drains any immediately
        available items without blocking.
        """
        batch: list[QueueItem] = []
        try:
            batch.append(self.get(block=True, timeout=timeout))
            while len(batch) < max_items:
                batch.append(self.get_nowait())
        except queue.Empty:
            pass
        return batch

    def filter_stale_batch(
        self, batch: list[QueueItem], now: float | None = None
    ) -> tuple[list[QueueItem], list[QueueItem]]:
        """Filter an in-flight batch against TTL, e.g. after a SQLite lock collision.

        Returns (valid_items, expired_items). Automatically calls super().task_done()
        for expired items and increments dropped_ttl_count.
        """
        current_time = time.monotonic() if now is None else now
        valid: list[QueueItem] = []
        expired: list[QueueItem] = []

        for item in batch:
            wrapped = item if isinstance(item, QueueItem) else QueueItem(payload=item)
            if wrapped.is_expired(self.ttl_seconds, now=current_time):
                expired.append(wrapped)
            else:
                valid.append(wrapped)

        if expired:
            with self._metrics_lock:
                self._dropped_ttl_count += len(expired)
            for _ in expired:
                super().task_done()

        return valid, expired

    def record_written(self, count: int = 1) -> None:
        """Record successful write count to underlying store."""
        with self._metrics_lock:
            self._total_written += max(0, int(count))

    def record_dropped_ttl(self, count: int = 1) -> None:
        """Manually record items dropped due to TTL expiration."""
        with self._metrics_lock:
            self._dropped_ttl_count += max(0, int(count))

    def record_lock_contention(self) -> None:
        """Record a database lock contention event (e.g. SQLite database is locked)."""
        with self._metrics_lock:
            self._lock_contention_events += 1

    @property
    def stats(self) -> dict[str, Any]:
        """Return queue health and throughput diagnostics."""
        with self._metrics_lock:
            return {
                "queue_depth": self.qsize(),
                "total_enqueued": self._total_enqueued,
                "total_written": self._total_written,
                "dropped_ttl_count": self._dropped_ttl_count,
                "dropped_capacity_count": self._dropped_capacity_count,
                "lock_contention_events": self._lock_contention_events,
                "maxsize": self.maxsize,
                "ttl_seconds": self.ttl_seconds,
            }
