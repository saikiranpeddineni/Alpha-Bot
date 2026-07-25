"""Unit tests for Spawn Capital Allocator (Spawn Army v2)."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from app.research.spawn.spawn_allocator import (
    SpawnCapitalAllocator,
    IncumbentScore,
    CandidateScore,
    AllocationDecision,
)


class MockPosition:
    def __init__(self, symbol: str, side: str, entry_price: float, initial_risk: float, entry_at: datetime, qty: float = 50.0, strategy: str = "orb"):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.initial_risk_per_share = initial_risk
        self.entry_at = entry_at
        self.qty = qty
        self.strategy = strategy


class MockVirtualPortfolio:
    def __init__(self, max_positions: int = 3):
        self.max_positions = max_positions
        self.positions = {}

    def can_open_position(self, symbol: str) -> bool:
        if symbol in self.positions:
            return False
        return len(self.positions) < self.max_positions


def test_feasibility_check():
    allocator = SpawnCapitalAllocator()
    vp = MockVirtualPortfolio(max_positions=2)
    
    # Empty portfolio: Can open
    assert allocator.check_feasibility(vp, "AAPL") is True
    
    # Add 1 position
    now = datetime.now(timezone.utc)
    vp.positions["AAPL"] = MockPosition("AAPL", "long", 150.0, 1.5, now)
    assert allocator.check_feasibility(vp, "MSFT") is True
    
    # Symbol already held: Cannot open
    assert allocator.check_feasibility(vp, "AAPL") is False
    
    # Portfolio full (2/2): Cannot open
    vp.positions["MSFT"] = MockPosition("MSFT", "long", 300.0, 3.0, now)
    assert allocator.check_feasibility(vp, "GOOGL") is False


def test_evaluate_candidate_scoring():
    allocator = SpawnCapitalAllocator()
    cand_obs = {
        "symbol": "AMD",
        "side": "long",
        "strategy": "momentum",
        "score": 0.80
    }
    
    score_obj = allocator.evaluate_candidate(cand_obs, capital_dollars=10000.0)
    assert score_obj.symbol == "AMD"
    assert score_obj.strategy == "momentum"
    assert score_obj.expected_r == 1.60  # 0.80 * 2.0
    assert score_obj.score > 1.0


def test_evaluate_incumbent_holding_penalty():
    allocator = SpawnCapitalAllocator(alpha_holding_penalty=0.01)
    now = datetime.now(timezone.utc)
    
    # Position held for 5 minutes (no holding penalty)
    pos_young = MockPosition("NVDA", "long", 100.0, 2.0, now - timedelta(minutes=5))
    inc_young = allocator.evaluate_incumbent(pos_young, current_price=101.0, now=now)
    assert inc_young.holding_mins == 5.0
    assert inc_young.holding_penalty == 0.0

    # Position held for 45 minutes (30 mins stagnant over 15m threshold)
    pos_old = MockPosition("NVDA", "long", 100.0, 2.0, now - timedelta(minutes=45))
    inc_old = allocator.evaluate_incumbent(pos_old, current_price=101.0, now=now)
    assert inc_old.holding_mins == 45.0
    assert inc_old.holding_penalty == pytest.approx(0.30)  # 0.01 * (45 - 15)
    assert inc_old.score < inc_young.score


def test_evaluate_allocation_free_slot():
    allocator = SpawnCapitalAllocator()
    vp = MockVirtualPortfolio(max_positions=3)
    now = datetime.now(timezone.utc)
    
    cand_obs = {"symbol": "TSLA", "side": "long", "strategy": "orb", "score": 0.75}
    decision = allocator.evaluate_allocation(vp, {}, cand_obs, now)
    
    assert decision.action == "SPAWN_NEW"
    assert decision.candidate_symbol == "TSLA"
    assert decision.reason == "FREE_SLOT_AVAILABLE"


def test_evaluate_allocation_rotation_guards():
    allocator = SpawnCapitalAllocator(
        min_incumbent_age_mins=15.0,
        max_incumbent_score_for_rotation=1.2,
        min_score_delta=0.10
    )
    vp = MockVirtualPortfolio(max_positions=1)
    now = datetime.now(timezone.utc)
    
    # 1. Incumbent too young (<15m) -> Rotation blocked
    pos_young = MockPosition("COHR", "long", 100.0, 2.0, now - timedelta(minutes=5))
    vp.positions["COHR"] = pos_young
    
    cand_obs = {"symbol": "SKHY", "side": "long", "strategy": "momentum", "score": 0.90}
    decision_young = allocator.evaluate_allocation(vp, {"COHR": 100.0}, cand_obs, now)
    assert decision_young.action == "NO_ACTION"
    assert "INCUMBENT_TOO_YOUNG" in decision_young.reason

    # 2. Incumbent trending strong (>1.2R) -> Rotation blocked
    pos_strong = MockPosition("COHR", "long", 100.0, 2.0, now - timedelta(minutes=30))
    vp.positions["COHR"] = pos_strong
    
    decision_strong = allocator.evaluate_allocation(vp, {"COHR": 103.0}, cand_obs, now) # +1.5R gain
    assert decision_strong.action == "NO_ACTION"
    assert "INCUMBENT_TRENDING_STRONG" in decision_strong.reason


def test_evaluate_allocation_rotation_success():
    allocator = SpawnCapitalAllocator(
        min_incumbent_age_mins=15.0,
        max_incumbent_score_for_rotation=1.2,
        min_score_delta=0.05
    )
    vp = MockVirtualPortfolio(max_positions=1)
    now = datetime.now(timezone.utc)
    
    # Incumbent stagnant for 40 mins (+0.1R gain)
    pos_stagnant = MockPosition("COHR", "long", 100.0, 2.0, now - timedelta(minutes=40))
    vp.positions["COHR"] = pos_stagnant
    
    cand_obs = {"symbol": "SKHY", "side": "long", "strategy": "momentum", "score": 0.85}
    decision = allocator.evaluate_allocation(vp, {"COHR": 100.2}, cand_obs, now)
    
    assert decision.action == "ROTATE_REPLACE"
    assert decision.candidate_symbol == "SKHY"
    assert decision.incumbent_symbol == "COHR"
    assert decision.score_delta >= 0.05
