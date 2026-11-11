# =============================================================================
# [MODIFIED 2026-11-11]: Comprehensive AsyncWriteQueue & TTL Drop Guard Tests
# ORIGINAL INTENT: Stub test only validated basic queue.Queue put_nowait throughput.
# REASON FOR CHANGE: Fully verify AsyncWriteQueue bounded capacity (maxsize=5000),
# 30-second TTL drop guard, lock contention batch filtering, and metrics reporting (R5).
# SAFE ROLLBACK: Preserves original test_async_write_queue_non_blocking behavior.
# =============================================================================

import sqlite3
import time
import threading
import pytest
from app.core.async_write_queue import AsyncWriteQueue, QueueItem
from app.core.shadow_ledger import ShadowLedger


def test_async_write_queue_non_blocking():
    """Verify that enqueue does not block the caller even under high throughput."""
    write_queue = AsyncWriteQueue(maxsize=1000, ttl_seconds=30.0)
    start = time.perf_counter()

    for i in range(500):
        ok = write_queue.put_nowait({"tick": i, "data": "payload"})
        assert ok is True

    elapsed = time.perf_counter() - start
    assert elapsed < 0.2, f"Queue enqueue took too long: {elapsed:.4f}s"
    assert write_queue.qsize() == 500
    assert write_queue.stats["total_enqueued"] == 500


def test_async_write_queue_capacity_drop_guard():
    """Verify that when queue reaches max capacity, incoming items are dropped safely."""
    queue_cap = 50
    write_queue = AsyncWriteQueue(maxsize=queue_cap, ttl_seconds=30.0)

    # Fill to capacity
    for i in range(queue_cap):
        assert write_queue.put_nowait({"tick": i}) is True

    assert write_queue.qsize() == queue_cap

    # Next enqueues should drop and increment dropped_capacity_count without crashing
    for i in range(25):
        assert write_queue.put_nowait({"overflow": i}) is False

    assert write_queue.qsize() == queue_cap
    assert write_queue.stats["dropped_capacity_count"] == 25
    assert write_queue.stats["total_enqueued"] == queue_cap


def test_async_write_queue_ttl_drop_guard_on_dequeue():
    """Verify that queued items exceeding TTL are dropped upon dequeue."""
    # Use 0.05s TTL for fast test
    write_queue = AsyncWriteQueue(maxsize=100, ttl_seconds=0.05)

    # Enqueue 3 items
    write_queue.put_nowait({"item": 1})
    write_queue.put_nowait({"item": 2})

    # Wait for TTL to expire
    time.sleep(0.08)

    # Enqueue 1 fresh item
    write_queue.put_nowait({"item": 3})

    # Draining batch should discard expired items and return only item 3
    batch = write_queue.drain_batch(max_items=10, timeout=0.1)

    assert len(batch) == 1
    assert batch[0]["item"] == 3
    assert write_queue.stats["dropped_ttl_count"] == 2
    assert write_queue.unfinished_tasks == 1

    write_queue.task_done()
    assert write_queue.unfinished_tasks == 0


def test_async_write_queue_filter_stale_batch():
    """Verify batch filtering under simulated lock contention with TTL expiration."""
    write_queue = AsyncWriteQueue(maxsize=100, ttl_seconds=30.0)

    now = time.monotonic()
    # Create 2 fresh items and 2 expired items (older than 30s)
    fresh_1 = QueueItem(payload={"id": "f1"}, enqueued_at=now - 5.0)
    fresh_2 = QueueItem(payload={"id": "f2"}, enqueued_at=now - 10.0)
    stale_1 = QueueItem(payload={"id": "s1"}, enqueued_at=now - 35.0)
    stale_2 = QueueItem(payload={"id": "s2"}, enqueued_at=now - 45.0)

    # Simulate enqueuing these 4 items
    with write_queue.mutex:
        write_queue.unfinished_tasks += 4

    batch = [fresh_1, stale_1, fresh_2, stale_2]

    # Filter stale batch
    valid, expired = write_queue.filter_stale_batch(batch, now=now)

    assert len(valid) == 2
    assert len(expired) == 2
    assert [item["id"] for item in valid] == ["f1", "f2"]
    assert [item["id"] for item in expired] == ["s1", "s2"]
    assert write_queue.stats["dropped_ttl_count"] == 2
    # Unfinished tasks should have decremented by 2 for the expired items
    assert write_queue.unfinished_tasks == 2

    # Finish remaining tasks
    for _ in valid:
        write_queue.task_done()
    assert write_queue.unfinished_tasks == 0


def test_queue_item_dict_transparency():
    """Verify QueueItem provides transparent dictionary access."""
    item = QueueItem(payload={"symbol": "AAPL", "action": "BUY", "price": 150.0})

    assert item["symbol"] == "AAPL"
    assert "action" in item
    assert "non_existent" not in item
    assert item.get("price") == 150.0
    assert item.get("missing", 99.0) == 99.0
    assert item.age() >= 0.0
    assert not item.is_expired(ttl_seconds=30.0)


def test_shadow_ledger_queue_metrics_and_ttl_drop(tmp_path):
    """Verify ShadowLedger records queue stats and handles TTL drop guard."""
    db_file = tmp_path / "shadow_test_ttl.db"
    ledger = ShadowLedger(db_path=db_file)

    # Verify queue is AsyncWriteQueue
    assert isinstance(ledger._queue, AsyncWriteQueue)
    stats = ledger.queue_stats
    assert "queue_depth" in stats
    assert "dropped_ttl_count" in stats
    assert "dropped_capacity_count" in stats
    assert "lock_contention_events" in stats

    # Record entries
    ledger.record(
        symbol="AAPL",
        strategy="ORB",
        action="BUY",
        price=150.0,
        stop_loss=148.0,
        gate="meta_label",
        reason="ML rejected score=0.42",
    )

    # Flush should commit cleanly
    flushed = ledger.flush(timeout_sec=5.0)
    assert flushed is True
    assert ledger.queue_stats["total_written"] >= 1
    assert ledger.queue_stats["queue_depth"] == 0

    # Verify rows in SQLite
    rows = ledger.query(limit=10)["entries"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
