from datetime import UTC, datetime, timedelta
import pytest
from app.portfolio.sandbox_spawner import SpawnedChildSandbox, SpawnedSandboxExecutor


def test_spawned_child_sandbox_initialization():
    spawned_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    child = SpawnedChildSandbox(policy_id="child_pol_1", spawned_at=spawned_at, seed_capital=20000.0)

    assert child.policy_id == "child_pol_1"
    assert child.seed_capital == 20000.0
    assert child.status == "ACTIVE"
    assert child.simulator.cfg.starting_capital == 20000.0


def test_causal_spawned_at_boundary():
    spawned_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    child = SpawnedChildSandbox(policy_id="child_pol_1", spawned_at=spawned_at, seed_capital=10000.0)

    before_ts = datetime(2026, 7, 10, 11, 59, tzinfo=UTC)
    after_ts = datetime(2026, 7, 10, 12, 1, tzinfo=UTC)

    assert child.is_eligible_at(before_ts) is False
    assert child.is_eligible_at(after_ts) is True

    # Signal before spawned_at should NOT be recorded
    child.record_signal({"symbol": "AVGO", "action": "ENTER_LONG", "price": 150.0}, before_ts)
    assert len(child.virtual_signals) == 0

    # Signal after spawned_at should be recorded
    child.record_signal({"symbol": "AVGO", "action": "ENTER_LONG", "price": 150.0}, after_ts)
    assert len(child.virtual_signals) == 1


def test_spawned_sandbox_executor_multi_child():
    rows = [
        {"policy_id": "pol_1", "spawned_at": "2026-07-01T00:00:00Z", "seed_capital": 15000.0, "status": "ACTIVE"},
        {"policy_id": "pol_2", "spawned_at": "2026-07-05T00:00:00Z", "seed_capital": 10000.0, "status": "ACTIVE"},
    ]
    executor = SpawnedSandboxExecutor(rows)
    assert len(executor.children) == 2

    t1 = datetime(2026, 7, 3, 14, 0, tzinfo=UTC)
    sig1 = {"symbol": "ORCL", "action": "ENTER_LONG", "price": 100.0, "timestamp": t1}

    executor.process_signal_for_children(sig1, t1)

    # pol_1 is active since July 1 -> gets sig1
    assert len(executor.children["pol_1"].virtual_signals) == 1
    # pol_2 is spawned on July 5 -> does NOT get sig1 at July 3
    assert len(executor.children["pol_2"].virtual_signals) == 0

    final_results = executor.finalize_all()
    assert "pol_1" in final_results
    assert "pol_2" in final_results
    assert final_results["pol_1"]["seed_capital"] == 15000.0
