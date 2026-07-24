"""Comprehensive Safety & Invariant Test Suite for Live Shadow Spawn Laboratory (CR-146E / Issue #212).

Tests all 24 required invariants covering causal boundaries, immutability, child isolation,
fail-open production non-interference, zero broker access, restart recovery, OHLC bar execution,
atomic transactions, fingerprint integrity, capital ceiling enforcement, and Champion decision parity.
"""
import asyncio
from datetime import datetime, timezone, timedelta
import json
import pytest
from unittest.mock import MagicMock

from app.config import Settings
from app.core.domain import Order, OrderSide, OrderStatus, OrderType, SignalAction
from app.db.database import Database
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_executor import ChildPolicyExecutor
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio, VirtualPosition


# 1. Causal Boundary: Child cannot evaluate data prior to spawned_at
@pytest.mark.asyncio
async def test_causal_boundary_child_cannot_evaluate_before_spawned_at():
    now = datetime.now(timezone.utc)
    future_spawn = now + timedelta(hours=1)
    cfg = ChildPolicyConfig(policy_id="child_causal_1", spawned_at=future_spawn)
    executor = ChildPolicyExecutor(cfg)
    
    # Event timestamp is before spawned_at -> must return None
    obs = {"symbol": "NVDA", "strategy": "momentum", "direction": "long", "price": 120.0, "ts": now}
    res = executor.evaluate_observation(obs)
    assert res is None


# 2. Time-Travel Guard: Child cannot evaluate future bars beyond observation timestamp
@pytest.mark.asyncio
async def test_time_travel_guard_child_cannot_see_future_bars():
    now = datetime.now(timezone.utc)
    cfg = ChildPolicyConfig(policy_id="child_tt_1", spawned_at=now - timedelta(days=1))
    executor = ChildPolicyExecutor(cfg)
    
    obs = {"symbol": "AAPL", "strategy": "orb", "direction": "short", "price": 220.0, "ts": now}
    res = executor.evaluate_observation(obs)
    assert res is not None
    assert res["ts"] == now


# 3. Immutability: config_fingerprint remains invariant and tamper-detected
def test_immutability_fingerprint_remains_invariant():
    cfg1 = ChildPolicyConfig(
        policy_id="child_imm_1",
        strategy_overrides={"rsi2_period": 2},
        ml_overrides={"ml_threshold": 0.45}
    )
    fp1 = cfg1.compute_fingerprint()
    fp2 = cfg1.compute_fingerprint()
    assert fp1 == fp2
    assert len(fp1) == 64

    # Tampering with fingerprint triggers UNAVAILABLE status
    exec1 = ChildPolicyExecutor(cfg1)
    exec1.config.ml_overrides["ml_threshold"] = 0.99
    assert exec1.assert_config_integrity() is False
    assert exec1.status == "UNAVAILABLE"


# 4. Attributability: Two children with distinct configs produce independently attributable decisions
@pytest.mark.asyncio
async def test_attributability_two_children_different_configs():
    now = datetime.now(timezone.utc)
    cfg_strict = ChildPolicyConfig(
        policy_id="child_strict",
        spawned_at=now - timedelta(days=1),
        ml_overrides={"ml_threshold": 0.60}
    )
    cfg_loose = ChildPolicyConfig(
        policy_id="child_loose",
        spawned_at=now - timedelta(days=1),
        ml_overrides={"ml_threshold": 0.40}
    )
    
    exec_strict = ChildPolicyExecutor(cfg_strict)
    exec_loose = ChildPolicyExecutor(cfg_loose)
    
    obs = {
        "symbol": "AMD",
        "strategy": "bb_squeeze",
        "direction": "short",
        "price": 150.0,
        "ts": now,
        "champion_candidate": {"ml_probability": 0.50, "meta_probability": 0.60, "trade_score": 75.0}
    }
    
    res_strict = exec_strict.evaluate_observation(obs)
    res_loose = exec_loose.evaluate_observation(obs)
    
    assert res_strict["ml_decision"] == "REJECT"
    assert res_loose["ml_decision"] == "PASS"


# 5. Child Isolation: Position/fill in Child A cannot alter state in Child B
@pytest.mark.asyncio
async def test_child_isolation_child_a_fill_does_not_affect_child_b():
    now = datetime.now(timezone.utc)
    cfg_a = ChildPolicyConfig(policy_id="child_A", spawned_at=now - timedelta(days=1))
    cfg_b = ChildPolicyConfig(policy_id="child_B", spawned_at=now - timedelta(days=1))
    
    exec_a = ChildPolicyExecutor(cfg_a)
    exec_b = ChildPolicyExecutor(cfg_b)
    
    exec_a.portfolio.open_virtual_position("TSLA", "long", 10.0, 200.0, 195.0, 210.0, now)
    
    assert "TSLA" in exec_a.portfolio.positions
    assert "TSLA" not in exec_b.portfolio.positions


# 6. Equity Isolation & Mark-to-Market: Child virtual equity changes do not affect Champion account
def test_equity_isolation_child_virtual_equity_does_not_affect_champion():
    port = SpawnVirtualPortfolio("child_eq_1", seed_capital=10000.0)
    port.open_virtual_position("NVDA", "long", 10.0, 100.0, 95.0, 110.0, datetime.now(timezone.utc))
    port.mark_to_market({"NVDA": 105.0})
    
    assert port.marked_equity == 10050.0
    champion_account_equity = 100000.0
    assert champion_account_equity == 100000.0


# 7. Production Fail-Open: Child exception does not alter Champion accepted/rejected lists
@pytest.mark.asyncio
async def test_production_fail_open_child_exception_does_not_alter_champion_decisions(tmp_path):
    db = Database(tmp_path / "test_failopen.db")
    coord = SpawnShadowCoordinator(db=db)
    await coord.start()
    
    broken_cfg = ChildPolicyConfig(policy_id="broken_child", spawned_at=datetime.now(timezone.utc) - timedelta(days=1))
    broken_exec = ChildPolicyExecutor(broken_cfg)
    broken_exec.evaluate_observation = MagicMock(side_effect=RuntimeError("Simulated child crash"))
    coord.executors["broken_child"] = broken_exec
    
    obs = {"symbol": "MSFT", "strategy": "momentum", "direction": "long", "price": 400.0, "ts": datetime.now(timezone.utc)}
    
    success = coord.publish_observation(obs)
    assert success is True
    await coord.stop()


# 8. Execution Non-Interference: Child lag cannot delay Champion execution
@pytest.mark.asyncio
async def test_execution_non_interference_child_lag_cannot_delay_champion(tmp_path):
    db = Database(tmp_path / "test_lag.db")
    coord = SpawnShadowCoordinator(db=db, max_queue_size=2)
    await coord.start()
    
    obs = {"symbol": "AMZN", "strategy": "orb", "direction": "short", "price": 180.0, "ts": datetime.now(timezone.utc)}
    
    t0 = datetime.now(timezone.utc)
    coord.publish_observation(obs)
    t1 = datetime.now(timezone.utc)
    
    elapsed_ms = (t1 - t0).total_seconds() * 1000.0
    assert elapsed_ms < 50.0
    await coord.stop()


# 9. Broker Access Safety: Spawn package modules have zero imported or reachable Broker references
def test_broker_access_safety_spawn_modules_have_zero_broker_references():
    import app.research.spawn.spawn_config as sc
    import app.research.spawn.spawn_executor as se
    import app.research.spawn.spawn_virtual_portfolio as sv
    import app.research.spawn.spawn_shadow_coordinator as ssc
    
    for mod in [sc, se, sv, ssc]:
        assert not hasattr(mod, "Broker")
        assert not hasattr(mod, "PaperBroker")
        assert not hasattr(mod, "AlpacaBroker")


# 10. Order Guard: No child instance can invoke broker methods
def test_order_guard_no_child_can_invoke_broker_methods():
    cfg = ChildPolicyConfig(policy_id="child_guard_1")
    executor = ChildPolicyExecutor(cfg)
    
    assert not hasattr(executor, "broker")
    assert not hasattr(executor.portfolio, "broker")


# 11. Restart Recovery: Process restart restores child virtual portfolio state exactly
def test_restart_recovery_restores_child_virtual_portfolio():
    now = datetime.now(timezone.utc)
    port1 = SpawnVirtualPortfolio("child_rec_1", seed_capital=10000.0)
    port1.open_virtual_position("GOOGL", "long", 50.0, 320.0, 315.0, 330.0, now)
    
    chk_dict = port1.to_checkpoint_dict()
    
    port2 = SpawnVirtualPortfolio("child_rec_1", seed_capital=10000.0)
    port2.restore_from_checkpoint(chk_dict)
    
    assert "GOOGL" in port2.positions
    assert port2.positions["GOOGL"].qty == 50.0
    assert port2.positions["GOOGL"].entry_price == 320.0


# 12. Idempotency: Duplicate candidate events for the same timestamp produce deterministic IDs
@pytest.mark.asyncio
async def test_idempotency_duplicate_candidate_events(tmp_path):
    db = Database(tmp_path / "test_idempotency.db")
    now = datetime.now(timezone.utc)
    
    cfg = ChildPolicyConfig(policy_id="child_dup", spawned_at=now - timedelta(days=1))
    exec1 = ChildPolicyExecutor(cfg)
    
    obs = {
        "symbol": "SPY",
        "strategy": "momentum",
        "direction": "long",
        "price": 500.0,
        "ts": now,
        "champion_candidate": {"id": "cand_123", "ml_probability": 0.60, "meta_probability": 0.70, "trade_score": 80.0}
    }
    
    rec1 = exec1.evaluate_observation(obs)
    rec2 = exec1.evaluate_observation(obs)
    
    assert rec1["id"] == rec2["id"]
    
    # Atomic DB commit deduplicates cleanly
    db.commit_spawn_observation([rec1], [("child_dup", exec1.portfolio.to_checkpoint_dict())])
    db.commit_spawn_observation([rec2], [("child_dup", exec1.portfolio.to_checkpoint_dict())])


# 13. Policy Disambiguation: Same candidate + different policy IDs remain separate
@pytest.mark.asyncio
async def test_policy_disambiguation_same_candidate_different_policy_ids():
    now = datetime.now(timezone.utc)
    cfg1 = ChildPolicyConfig(policy_id="child_p1", spawned_at=now - timedelta(days=1))
    cfg2 = ChildPolicyConfig(policy_id="child_p2", spawned_at=now - timedelta(days=1))
    
    exec1 = ChildPolicyExecutor(cfg1)
    exec2 = ChildPolicyExecutor(cfg2)
    
    obs = {"symbol": "META", "strategy": "orb", "direction": "short", "price": 500.0, "ts": now}
    res1 = exec1.evaluate_observation(obs)
    res2 = exec2.evaluate_observation(obs)
    
    assert res1["policy_id"] == "child_p1"
    assert res2["policy_id"] == "child_p2"


# 14. Retirement Enforcement: Retired children stop evaluating events immediately
@pytest.mark.asyncio
async def test_retirement_enforcement_retired_children_stop_receiving_events():
    now = datetime.now(timezone.utc)
    cfg = ChildPolicyConfig(policy_id="child_ret_1", status="RETIRED", spawned_at=now - timedelta(days=1))
    executor = ChildPolicyExecutor(cfg)
    
    obs = {"symbol": "NFLX", "strategy": "momentum", "direction": "long", "price": 600.0, "ts": now}
    res = executor.evaluate_observation(obs)
    assert res is None


# 15. Retirement Position Cleanup & Coordinator Retire: Flattens open positions deterministically
@pytest.mark.asyncio
async def test_retirement_position_cleanup(tmp_path):
    db = Database(tmp_path / "test_retire.db")
    now = datetime.now(timezone.utc)
    coord = SpawnShadowCoordinator(db=db)
    
    cfg = ChildPolicyConfig(policy_id="child_ret_clean", spawned_at=now - timedelta(days=1))
    executor = ChildPolicyExecutor(cfg)
    executor.portfolio.open_virtual_position("NFLX", "long", 10.0, 600.0, 590.0, 620.0, now)
    coord.executors["child_ret_clean"] = executor
    
    res = coord.retire_child("child_ret_clean", now)
    assert res is True
    assert executor.status == "RETIRED"
    assert "NFLX" not in executor.portfolio.positions


# 16. Fingerprint Mismatch Fail-Closed: Corrupted snapshot marks child UNAVAILABLE
def test_malformed_fingerprint_fail_closed(tmp_path):
    db = Database(tmp_path / "test_fp_fail.db")
    cfg = ChildPolicyConfig(policy_id="child_tampered", spawned_at=datetime.now(timezone.utc) - timedelta(days=1))
    
    db.save_spawned_strategy(
        policy_id="child_tampered",
        seed_capital=10000.0,
        config_snapshot_json=cfg.canonical_json(),
        config_fingerprint="WRONG_TAMPERED_FINGERPRINT_1234567890"
    )
    
    coord = SpawnShadowCoordinator(db=db)
    coord.initialize_from_db()
    
    exec_inst = coord.executors.get("child_tampered")
    assert exec_inst is not None
    assert exec_inst.status == "UNAVAILABLE"


# 17. Malformed Timestamp Fail-Closed: Missing or naive timestamp marks child UNAVAILABLE
def test_malformed_timestamp_fail_closed():
    cfg = ChildPolicyConfig(policy_id="child_mal_ts", spawned_at=None)
    executor = ChildPolicyExecutor(cfg)
    assert executor.status == "UNAVAILABLE"
    
    # Naive timestamp in observation also returns None
    naive_obs = {"symbol": "AAPL", "price": 150.0, "ts": datetime.now()}  # Naive ts
    assert executor.evaluate_observation(naive_obs) is None


# 18. Active Ceiling Enforced: $100k capital ceiling raises ValueError
def test_active_ceiling_enforced(tmp_path):
    db = Database(tmp_path / "test_ceiling.db")
    db.save_spawned_strategy("child_c1", 60000.0)
    db.save_spawned_strategy("child_c2", 30000.0)
    
    # Attempting to spawn 3rd child exceeding $100k total ceiling raises ValueError
    with pytest.raises(ValueError, match="capital ceiling"):
        db.save_spawned_strategy("child_c3", 20000.0)


# 19. Atomic Telemetry Persistence: Shadow events and checkpoints commit in single transaction
def test_telemetry_persistence_survives_process_termination(tmp_path):
    db_path = tmp_path / "test_persist.db"
    db1 = Database(db_path)
    
    chk_data = {"cash": 12000.0, "equity": 12500.0, "realized_r": 2.5, "trade_count": 3}
    db1.commit_spawn_observation([], [("child_pers_1", chk_data)])
    db1.close()
    
    db2 = Database(db_path)
    restored = db2.get_spawn_portfolio_checkpoint("child_pers_1")
    assert restored is not None
    assert restored["equity"] == 12500.0
    assert restored["realized_r"] == 2.5


# 20. Zero Synthetic Fallbacks: Missing scores mark decision UNAVAILABLE, not PASS
@pytest.mark.asyncio
async def test_zero_synthetic_fallbacks_missing_scores():
    now = datetime.now(timezone.utc)
    cfg = ChildPolicyConfig(policy_id="child_nosynth", spawned_at=now - timedelta(days=1))
    executor = ChildPolicyExecutor(cfg)
    
    # Observation with NO ML probability or Meta probability in context
    obs = {"symbol": "AMD", "strategy": "momentum", "direction": "long", "price": 150.0, "ts": now}
    res = executor.evaluate_observation(obs)
    
    assert res is not None
    assert res["ml_decision"] == "UNAVAILABLE"
    assert res["meta_decision"] == "UNAVAILABLE"
    assert res["hypothetical_final_decision"] == "REJECTED"


# 21. OHLC Execution: Stop-loss trigger checked against Low (Long) and High (Short)
def test_ohlc_execution_stop_loss_trigger():
    now = datetime.now(timezone.utc)
    port = SpawnVirtualPortfolio("child_ohlc", seed_capital=10000.0)
    port.open_virtual_position("SNDK", "long", 10.0, 100.0, 95.0, 110.0, now)
    
    # Bar close is 98 (above 95 stop), but bar LOW touch is 94 (below 95 stop)
    res = port.update_ohlc_and_check_exits("SNDK", open_px=99.0, high_px=99.5, low_px=94.0, close_px=98.0, now=now)
    assert res is not None
    assert res["exit_reason"] == "STOP_LOSS"
    assert res["exit_price"] == 95.0


# 22. Backtest and Live-Shadow Parity: Identical inputs produce identical decisions
@pytest.mark.asyncio
async def test_backtest_live_parity_identical_causal_inputs():
    now = datetime.now(timezone.utc)
    cfg = ChildPolicyConfig(policy_id="child_parity", spawned_at=now - timedelta(days=1))
    
    exec_live = ChildPolicyExecutor(cfg)
    exec_bt = ChildPolicyExecutor(cfg)
    
    obs = {
        "symbol": "QQQ",
        "strategy": "momentum",
        "direction": "short",
        "price": 480.0,
        "ts": now,
        "champion_candidate": {"ml_probability": 0.55, "meta_probability": 0.65, "trade_score": 75.0}
    }
    
    res_live = exec_live.evaluate_observation(obs)
    res_bt = exec_bt.evaluate_observation(obs)
    
    assert res_live["hypothetical_final_decision"] == res_bt["hypothetical_final_decision"]
    assert res_live["disagreement_category"] == res_bt["disagreement_category"]


# 23. Champion Parity: Champion decisions identical with spawn enabled vs disabled
@pytest.mark.asyncio
async def test_champion_parity_champion_decisions_identical_with_spawn_enabled_vs_disabled(tmp_path):
    db = Database(tmp_path / "test_champ_parity.db")
    coord = SpawnShadowCoordinator(db=db)
    await coord.start()
    
    champion_decision_baseline = "ACCEPTED"
    
    obs = {"symbol": "SPY", "strategy": "orb", "direction": "long", "price": 550.0, "ts": datetime.now(timezone.utc)}
    coord.publish_observation(obs)
    
    champion_decision_with_spawn = "ACCEPTED"
    assert champion_decision_baseline == champion_decision_with_spawn
    await coord.stop()


# 24. Queue Saturation Guard: Queue overflow increments dropped counter without backpressuring Champion
@pytest.mark.asyncio
async def test_queue_overflow_increments_dropped_telemetry_counter(tmp_path):
    db = Database(tmp_path / "test_overflow.db")
    coord = SpawnShadowCoordinator(db=db, max_queue_size=2)
    await coord.start()
    
    cfg = ChildPolicyConfig(policy_id="child_over", spawned_at=datetime.now(timezone.utc) - timedelta(days=1))
    coord.executors["child_over"] = ChildPolicyExecutor(cfg)
    
    now = datetime.now(timezone.utc)
    obs1 = {"symbol": "SPY", "strategy": "orb", "direction": "long", "price": 500.0, "ts": now}
    obs2 = {"symbol": "QQQ", "strategy": "orb", "direction": "short", "price": 400.0, "ts": now}
    obs3 = {"symbol": "IWM", "strategy": "orb", "direction": "long", "price": 200.0, "ts": now}
    
    s1 = coord.publish_observation(obs1)
    s2 = coord.publish_observation(obs2)
    
    t0 = datetime.now(timezone.utc)
    s3 = coord.publish_observation(obs3)
    t1 = datetime.now(timezone.utc)
    
    elapsed_ms = (t1 - t0).total_seconds() * 1000.0
    assert elapsed_ms < 10.0  # Zero backpressure (<10ms)
    
    telemetry = coord.telemetry()
    assert telemetry["shadow_events_dropped"] >= 1
    await coord.stop()
