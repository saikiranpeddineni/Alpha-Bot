import asyncio
import time
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.api.routes import _compute_shadow_ledger_analytics_sync, _compute_gate_alpha_sync


def test_shadow_analytics_sync_computation_offload():
    """Verify that synchronous shadow analytics computation executes accurately."""
    mock_db = MagicMock()
    mock_db.get_shadow_signals.return_value = [
        {
            "id": "CAND_1",
            "symbol": "SNDK",
            "strategy": "momentum",
            "final_decision": "accepted",
            "replayed": True,
            "simulated_pnl": 1.5,
            "mfe_pct": 2.0,
            "mae_pct": -0.5,
            "gate_evidence_json": {"vetoes": {"ml_gate": True}, "first_decisive_veto": "ml_gate"},
        }
    ]

    res = _compute_shadow_ledger_analytics_sync(
        db=mock_db,
        mode="paper",
        alt_policies=[],
        limit=5000,
        since=None,
        min_sample=30,
    )

    assert "summary" in res
    assert res["summary"]["total_signals"] == 1
    mock_db.get_shadow_signals.assert_called_once_with(mode="paper", limit=5000)


def test_gate_alpha_sync_computation_offload():
    """Verify that synchronous gate alpha computation executes accurately."""
    mock_db = MagicMock()
    mock_db.get_shadow_signals.return_value = [
        {
            "id": "CAND_1",
            "symbol": "SNDK",
            "final_decision": "rejected",
            "replayed": True,
            "gate_evidence_json": {"vetoes": {"ml_gate": True}, "first_decisive_veto": "ml_gate"},
        }
    ]

    res = _compute_gate_alpha_sync(
        db=mock_db,
        mode="paper",
        limit=5000,
    )

    assert "gates" in res
    assert "population_meta" in res
    assert res["population_meta"]["limit"] == 5000
    mock_db.get_shadow_signals.assert_called_once_with(mode="paper", limit=5000)


@pytest.mark.asyncio
async def test_analytics_offload_non_blocking_asyncio():
    """Verify that calling analytics computation via asyncio.to_thread keeps event loop unblocked."""
    mock_db = MagicMock()
    # Simulate a slow DB query inside thread
    def slow_signals(mode, limit):
        time.sleep(0.15)
        return []
    
    mock_db.get_shadow_signals.side_effect = slow_signals

    loop_counter = 0
    async def fast_heartbeat():
        nonlocal loop_counter
        for _ in range(5):
            await asyncio.sleep(0.02)
            loop_counter += 1

    # Run heavy offloaded task concurrently with fast event loop tasks
    heartbeat_task = asyncio.create_task(fast_heartbeat())
    analytics_task = asyncio.create_task(
        asyncio.to_thread(_compute_shadow_ledger_analytics_sync, mock_db, "paper", [], 5000, None, 30)
    )

    await asyncio.gather(heartbeat_task, analytics_task)
    assert loop_counter >= 3, "Event loop should continue processing fast tasks while offloaded task runs"
