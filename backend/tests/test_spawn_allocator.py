"""Unit tests for Spawn Capital Allocator V2 (Dollar Velocity & Release Feasibility)."""
import pytest
from datetime import datetime, timezone, timedelta

from app.research.spawn.spawn_allocator import (
    SpawnCapitalAllocator,
    IncumbentScore,
    CandidateScore,
    AllocationDecision,
)


class MockPosition:
    def __init__(self, symbol: str, side: str, entry_price: float, initial_risk: float, entry_at: datetime, qty: float = 50.0, strategy: str = "orb", initial_target: float = None):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.initial_risk_per_share = initial_risk
        self.entry_at = entry_at
        self.qty = qty
        self.strategy = strategy
        self.initial_target = initial_target or (entry_price * 1.02 if side == "long" else entry_price * 0.98)


class MockVirtualPortfolio:
    def __init__(self, max_positions: int = 2):
        self.max_open_positions = max_positions
        self.positions = {}

    def can_open_position(self, symbol: str) -> bool:
        if symbol in self.positions:
            return False
        return len(self.positions) < self.max_open_positions


def test_can_open_after_releasing():
    allocator = SpawnCapitalAllocator()
    vp = MockVirtualPortfolio(max_positions=2)
    now = datetime.now(timezone.utc)
    
    # Fill portfolio 2/2
    vp.positions["AAPL"] = MockPosition("AAPL", "long", 150.0, 1.5, now)
    vp.positions["MSFT"] = MockPosition("MSFT", "long", 300.0, 3.0, now)
    
    # Direct can_open_position is False
    assert vp.can_open_position("GOOGL") is False
    
    # Feasibility WITH incumbent release is True!
    assert allocator.can_open_after_releasing(vp, "GOOGL", "AAPL") is True
    assert allocator.check_feasibility(vp, "GOOGL", "AAPL") is True
    
    # Cannot release itself (symbol already held)
    assert allocator.can_open_after_releasing(vp, "AAPL", "AAPL") is False


def test_evaluate_candidate_dollar_velocity():
    allocator = SpawnCapitalAllocator()
    cand_obs = {
        "symbol": "AMD",
        "side": "long",
        "strategy": "momentum",
        "score": 0.80
    }
    
    score_obj = allocator.evaluate_candidate(cand_obs, capital_dollars=10000.0)
    assert score_obj.symbol == "AMD"
    assert score_obj.expected_net_dollars > 100.0
    assert score_obj.net_dollar_velocity > 0.0


def test_evaluate_incumbent_holding_decay():
    allocator = SpawnCapitalAllocator(alpha_stagnation_decay=0.005)
    now = datetime.now(timezone.utc)
    
    # Position held for 5 minutes (no stagnant decay)
    pos_young = MockPosition("NVDA", "long", 100.0, 2.0, now - timedelta(minutes=5), initial_target=105.0)
    inc_young = allocator.evaluate_incumbent(pos_young, current_price=101.0, now=now)
    assert inc_young.holding_mins == 5.0

    # Position held for 45 minutes (stagnant decay applied)
    pos_old = MockPosition("NVDA", "long", 100.0, 2.0, now - timedelta(minutes=45), initial_target=105.0)
    inc_old = allocator.evaluate_incumbent(pos_old, current_price=101.0, now=now)
    assert inc_old.holding_mins == 45.0
    assert inc_old.score < inc_young.score


def test_evaluate_allocation_rotation_success_with_release():
    allocator = SpawnCapitalAllocator(
        min_incumbent_age_mins=15.0,
        min_score_delta=0.001
    )
    vp = MockVirtualPortfolio(max_positions=1)
    now = datetime.now(timezone.utc)
    
    # Stagnant incumbent near target
    pos_stagnant = MockPosition("COHR", "long", 100.0, 2.0, now - timedelta(minutes=40), initial_target=100.5)
    vp.positions["COHR"] = pos_stagnant
    
    cand_obs = {"symbol": "SKHY", "side": "long", "strategy": "momentum", "score": 0.85}
    decision = allocator.evaluate_allocation(vp, {"COHR": 100.4}, cand_obs, now)
    
    assert decision.action == "ROTATE_REPLACE"
    assert decision.candidate_symbol == "SKHY"
    assert decision.incumbent_symbol == "COHR"
    assert decision.score_delta > 0.0
