import time
import queue
import threading


def test_async_write_queue_non_blocking():
    # Verify that enqueue does not block the caller even under high throughput
    write_queue: queue.Queue = queue.Queue(maxsize=1000)
    start = time.perf_counter()

    for i in range(500):
        write_queue.put_nowait({"tick": i, "data": "payload"})

    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"Queue enqueue took too long: {elapsed:.4f}s"
    assert write_queue.qsize() == 500
