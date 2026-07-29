"""Integration tests for Spawn Shadow Sidecar Zero-Broker Guarantee and Post-Risk Dispatch."""
import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator
from app.research.spawn.spawn_config import ChildPolicyConfig


@pytest.mark.asyncio
async def test_spawn_shadow_coordinator_zero_broker_boundary():
    coordinator = SpawnShadowCoordinator(max_queue_size=100)
    
    # Verify coordinator has zero references to broker execution or live ordering
    assert not hasattr(coordinator, "broker")
    assert not hasattr(coordinator, "submit_order")
    assert not hasattr(coordinator, "execute_live_trade")
    
    # Start coordinator
    await coordinator.start()
    assert coordinator.running is True
    assert coordinator.telemetry()["active_children"] >= 0

    # Publish post-risk approved candidate observation
    cand_obs = {
        "symbol": "AMD",
        "side": "long",
        "strategy": "momentum",
        "ts": datetime.now(timezone.utc),
        "champion_candidate": {"id": "cand_amd_001", "score": 0.85}
    }
    
    # Sidecar non-blocking queue processing
    published = coordinator.publish_observation(cand_obs)
    assert published is True
    
    await coordinator.stop()


@pytest.mark.asyncio
async def test_spawn_sidecar_non_blocking_behavior():
    coordinator = SpawnShadowCoordinator(max_queue_size=2)
    coordinator.running = True
    coordinator.executors = {"dummy": MagicMock()}
    
    # Fill queue to capacity (2 items)
    coordinator.queue.put_nowait({"type": "bar", "data": {}})
    coordinator.queue.put_nowait({"type": "bar", "data": {}})
    
    # Additional event should drop gracefully without blocking or throwing exception
    cand_obs = {"symbol": "NVDA", "side": "long", "ts": datetime.now(timezone.utc)}
    
    published = coordinator.publish_observation(cand_obs)
    assert published is False  # Dropped because queue full
    assert coordinator.shadow_events_dropped == 1


def test_spawn_retirement_fails_closed_without_current_mark():
    coordinator = SpawnShadowCoordinator()
    executor = MagicMock()
    executor.status = "ACTIVE"
    executor.portfolio.positions = {"NVDA": MagicMock()}
    executor.portfolio.last_prices = {}
    executor._finite.return_value = None
    coordinator.executors = {"spawn_test": executor}

    retired = coordinator.retire_child("spawn_test", datetime.now(timezone.utc))

    assert retired is False
    assert executor.status == "ACTIVE"
    executor.portfolio.close_virtual_position.assert_not_called()
    assert coordinator.children_retired == 0


def test_spawn_retirement_preflights_all_marks_before_any_close():
    coordinator = SpawnShadowCoordinator()
    executor = MagicMock()
    executor.status = "ACTIVE"
    executor.portfolio.positions = {"NVDA": MagicMock(), "AMD": MagicMock()}
    executor.portfolio.last_prices = {"NVDA": 120.0}
    executor._finite.side_effect = lambda value: float(value) if value is not None else None
    coordinator.executors = {"spawn_test": executor}

    retired = coordinator.retire_child("spawn_test", datetime.now(timezone.utc))

    assert retired is False
    assert executor.status == "ACTIVE"
    executor.portfolio.close_virtual_position.assert_not_called()
    assert coordinator.children_retired == 0
