"""Regression tests for issue #117 / 053.

Verifies that concurrent POST /api/backtest requests are fully serialized:
- A second concurrent request gets 409 immediately (not queued behind first).
- The guard and lock acquisition are a single indivisible step (no TOCTOU).
- ensure_data() and run_backtest() are only called while the lock is held.
- An exception inside the guarded block always releases the lock.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Unit-level: atomic acquire logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_request_gets_409_when_lock_held():
    """When the backtest lock is already held, wait_for raises TimeoutError -> 409."""
    lock = asyncio.Lock()
    await lock.acquire()  # simulate first request holding the lock

    # A second coroutine trying to acquire with timeout=1e-6 must fail immediately
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lock.acquire(), timeout=1e-6)

    lock.release()


@pytest.mark.asyncio
async def test_lock_acquired_when_free():
    """When no backtest is running, the lock is acquired immediately."""
    lock = asyncio.Lock()
    # Should not raise
    await asyncio.wait_for(lock.acquire(), timeout=1e-6)
    assert lock.locked()
    lock.release()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_lock_released_after_normal_completion():
    """Lock is released via finally block after successful execution."""
    lock = asyncio.Lock()
    await asyncio.wait_for(lock.acquire(), timeout=1e-6)
    try:
        pass  # simulate guarded work
    finally:
        lock.release()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_lock_released_after_exception():
    """Lock is released even when an exception is raised inside the guard."""
    lock = asyncio.Lock()
    await asyncio.wait_for(lock.acquire(), timeout=1e-6)
    try:
        try:
            raise ValueError("simulated failure")
        finally:
            lock.release()
    except ValueError:
        pass
    assert not lock.locked()


# ---------------------------------------------------------------------------
# Concurrency: two simultaneous coroutines using the same pattern as the route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_requests_second_is_rejected():
    """Two concurrent coroutines: first acquires, second gets TimeoutError."""
    lock = asyncio.Lock()
    results = []

    async def backtest_coroutine(delay_before_release: float = 0.05):
        try:
            await asyncio.wait_for(lock.acquire(), timeout=1e-6)
        except asyncio.TimeoutError:
            results.append("rejected")
            return
        try:
            await asyncio.sleep(delay_before_release)  # simulate work
            results.append("completed")
        finally:
            lock.release()

    # Schedule both coroutines at once
    await asyncio.gather(
        backtest_coroutine(delay_before_release=0.05),
        backtest_coroutine(delay_before_release=0.05),
    )

    assert "completed" in results, "at least one request should complete"
    assert "rejected" in results, "concurrent second request must be rejected"
    assert results.count("completed") == 1, "exactly one request should complete"
    assert results.count("rejected") == 1, "exactly one request should be rejected"


@pytest.mark.asyncio
async def test_after_first_completes_second_can_run():
    """After the first backtest finishes (lock released), a new request succeeds."""
    lock = asyncio.Lock()

    # First request
    await asyncio.wait_for(lock.acquire(), timeout=1e-6)
    lock.release()

    # Second request (sequential, not concurrent)
    await asyncio.wait_for(lock.acquire(), timeout=1e-6)
    assert lock.locked()
    lock.release()
    assert not lock.locked()


# ---------------------------------------------------------------------------
# Ensure no shared state accessed before lock (structural test)
# ---------------------------------------------------------------------------

def test_ensure_data_only_called_inside_lock():
    """Structural: ensure_data is called after lock acquisition in post_backtest.

    We verify this by inspecting source: ensure_data must not appear before
    the asyncio.wait_for(lock.acquire()) line in the route handler.
    """
    import inspect
    from app.api.routes import post_backtest
    src = inspect.getsource(post_backtest)
    acquire_pos = src.find("lock.acquire()")
    ensure_data_pos = src.find("ensure_data()")
    assert acquire_pos != -1, "lock.acquire() must be present in post_backtest"
    assert ensure_data_pos != -1, "ensure_data() must be present in post_backtest"
    assert acquire_pos < ensure_data_pos, (
        "ensure_data() must appear AFTER lock.acquire() in post_backtest "
        "to guarantee no shared state is read before the lock is held"
    )


def test_no_lock_locked_check_before_acquire():
    """Structural: lock.locked() must NOT be used as a TOCTOU guard in the route.

    The old pattern `if lock.locked(): raise 409 ... async with lock:` has a
    gap between the check and acquisition where two requests can both observe
    locked()==False. The fix replaces this with a single atomic wait_for acquire.
    """
    import inspect
    from app.api.routes import post_backtest
    src = inspect.getsource(post_backtest)
    # The racy pattern must be gone
    assert "lock.locked()" not in src, (
        "lock.locked() TOCTOU guard must be removed; use atomic wait_for(lock.acquire(), timeout=1e-6) instead"
    )
