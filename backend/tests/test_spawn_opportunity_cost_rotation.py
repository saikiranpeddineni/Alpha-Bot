from datetime import datetime, timedelta, timezone
import pytest
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio
from app.research.spawn.spawn_opportunity_cost import (
    evaluate_incumbent,
    evaluate_incoming,
    check_rotation_opportunity,
)

def make_portfolio(**risk_overrides):
    cfg = {"max_open_positions": 1}
    cfg.update(risk_overrides)
    return SpawnVirtualPortfolio("test_rot", 10000.0, cfg)

def test_disabled_by_default_preserves_baseline():
    p = make_portfolio()
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    p.open_virtual_position("MU", "short", 10, 100.0, 102.0, 90.0, now)
    
    cand = {"symbol": "INTC", "side": "short", "score": 0.90}
    rec = p.check_and_execute_rotation(cand, now + timedelta(minutes=15))
    assert rec is None
    assert "MU" in p.positions

def test_rotation_replaces_weak_incumbent_when_edge_exceeded():
    p = make_portfolio(rotation_enabled=True, rotation_min_edge=0.40, rotation_max_incumbent_quality=0.20)
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    p.open_virtual_position("MU", "short", 10, 100.0, 102.0, 90.0, now)
    
    # After 40 minutes with price stagnant at 100.1 (negative R / duration penalty)
    now_later = now + timedelta(minutes=40)
    p.last_prices["MU"] = 100.1
    cand = {"symbol": "INTC", "side": "short", "score": 0.85}

    rec = p.check_and_execute_rotation(cand, now_later)
    assert rec is not None
    assert rec["exit_reason"] == "OPPORTUNITY_ROTATION_EXIT"
    assert "MU" not in p.positions
    assert "rotation_telemetry" in rec
    telemetry = rec["rotation_telemetry"]
    assert telemetry["incumbent_symbol"] == "MU"
    assert telemetry["incoming_symbol"] == "INTC"
    assert telemetry["score_delta"] >= 0.40

def test_strong_incumbent_is_protected_from_rotation():
    p = make_portfolio(rotation_enabled=True, rotation_min_edge=0.40, rotation_max_incumbent_quality=0.20)
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    p.open_virtual_position("MU", "short", 10, 100.0, 102.0, 90.0, now)

    # MU moved in favor by 1.5 R (to 97.0)
    now_later = now + timedelta(minutes=15)
    p.last_prices["MU"] = 97.0
    cand = {"symbol": "INTC", "side": "short", "score": 0.95}

    rec = p.check_and_execute_rotation(cand, now_later)
    assert rec is None
    assert "MU" in p.positions

def test_stop_loss_takes_priority_over_rotation():
    p = make_portfolio(rotation_enabled=True, rotation_min_edge=0.20)
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    p.open_virtual_position("MU", "short", 10, 100.0, 102.0, 90.0, now)

    # Bar high breaks stop loss (102.5 >= 102.0)
    bar_exit = p.update_ohlc_and_check_exits("MU", 100.0, 102.5, 99.5, 102.1, now + timedelta(minutes=10))
    assert bar_exit is not None
    assert bar_exit["exit_reason"] == "STOP_LOSS"
    assert "MU" not in p.positions

def test_rotation_opens_new_position_when_capacity_full():
    p = make_portfolio(rotation_enabled=True, rotation_min_edge=0.30, rotation_max_incumbent_quality=0.10)
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    p.open_virtual_position("MU", "short", 10, 100.0, 102.0, 90.0, now)
    assert len(p.positions) == 1

    now_later = now + timedelta(minutes=35)
    p.last_prices["MU"] = 100.2
    cand = {"symbol": "INTC", "side": "short", "score": 0.85}

    # Open INTC position — rotation should displace MU first, then open INTC
    new_pos = p.open_virtual_position("INTC", "short", 10, 50.0, 51.0, 45.0, now_later, candidate_obs=cand)
    assert new_pos is not None
    assert "MU" not in p.positions
    assert "INTC" in p.positions
    assert len(p.positions) == 1

def test_zero_broker_authority():
    import inspect
    from app.research.spawn import spawn_opportunity_cost
    source = inspect.getsource(spawn_opportunity_cost)
    assert "broker." not in source
    assert "submit_order" not in source
