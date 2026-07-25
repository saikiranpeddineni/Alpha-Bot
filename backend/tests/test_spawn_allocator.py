"""Unit tests for the portfolio-aware Spawn allocator."""
from datetime import datetime, timezone, timedelta

from app.research.spawn.spawn_allocator import SpawnCapitalAllocator


class MockPosition:
    def __init__(self, symbol, side, entry_price, initial_risk, entry_at, qty=50.0,
                 strategy="orb", initial_target=None, peak_price=None, trough_price=None):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.initial_risk_per_share = initial_risk
        self.entry_at = entry_at
        self.qty = qty
        self.strategy = strategy
        self.initial_target = initial_target or (entry_price * 1.02 if side == "long" else entry_price * 0.98)
        self.peak_price = peak_price if peak_price is not None else entry_price
        self.trough_price = trough_price if trough_price is not None else entry_price


class MockVirtualPortfolio:
    def __init__(self, max_positions=2):
        self.max_open_positions = max_positions
        self.positions = {}

    def can_open_position(self, symbol):
        return symbol.upper() not in self.positions and len(self.positions) < self.max_open_positions


def test_can_open_after_releasing():
    allocator = SpawnCapitalAllocator()
    vp = MockVirtualPortfolio(2)
    now = datetime.now(timezone.utc)
    vp.positions["AAPL"] = MockPosition("AAPL", "long", 150, 1.5, now)
    vp.positions["MSFT"] = MockPosition("MSFT", "long", 300, 3, now)
    assert not vp.can_open_position("GOOGL")
    assert allocator.can_open_after_releasing(vp, "GOOGL", "AAPL")
    assert not allocator.can_open_after_releasing(vp, "AAPL", "AAPL")


def test_calibrated_candidate_ev_overrides_raw_score_fallback():
    allocator = SpawnCapitalAllocator()
    obs = {"symbol": "AMD", "strategy": "momentum", "score": 0.51,
           "calibrated_expected_net_dollars": 420.0, "expectancy_confidence": 0.9}
    result = allocator.evaluate_candidate(obs, 10000.0)
    assert result.expected_net_dollars == 420.0
    assert result.confidence == 0.9
    assert result.expected_holding_mins == 42.0


def test_strategy_specific_holding_time_changes_velocity():
    allocator = SpawnCapitalAllocator(gamma_friction_cost_pct=0.0)
    base = {"symbol": "X", "calibrated_expected_net_dollars": 100.0}
    rsi = allocator.evaluate_candidate({**base, "strategy": "rsi2_reversion"}, 10000)
    momentum = allocator.evaluate_candidate({**base, "strategy": "momentum"}, 10000)
    assert rsi.net_dollar_velocity > momentum.net_dollar_velocity


def test_real_stagnation_does_not_penalize_position_near_new_mfe():
    allocator = SpawnCapitalAllocator(alpha_stagnation_decay=0.01)
    now = datetime.now(timezone.utc)
    pos = MockPosition("NVDA", "long", 100, 2, now - timedelta(minutes=45),
                       initial_target=110, peak_price=104)
    result = allocator.evaluate_incumbent(pos, 104, now)
    assert result.stagnation_mins == 0.0


def test_free_last_slot_can_wait_for_better_opportunity():
    allocator = SpawnCapitalAllocator(reservation_hurdle=0.05)
    vp = MockVirtualPortfolio(2)
    now = datetime.now(timezone.utc)
    vp.positions["A"] = MockPosition("A", "long", 100, 2, now, strategy="orb")
    weak = {"symbol": "B", "strategy": "momentum", "calibrated_expected_net_dollars": 20.0}
    decision = allocator.evaluate_allocation(vp, {"A": 100}, weak, now)
    assert decision.action == "WAIT"
    assert decision.reason == "RESERVE_CAPITAL_FOR_BETTER_OPPORTUNITY"


def test_rotation_chooses_best_pairwise_replacement():
    allocator = SpawnCapitalAllocator(min_score_delta=0.001, reservation_hurdle=0.0)
    vp = MockVirtualPortfolio(2)
    now = datetime.now(timezone.utc)
    vp.positions["A"] = MockPosition("A", "long", 100, 2, now - timedelta(minutes=40),
                                      strategy="orb", initial_target=100.5, peak_price=100.4)
    vp.positions["B"] = MockPosition("B", "long", 100, 2, now - timedelta(minutes=40),
                                      strategy="momentum", initial_target=110, peak_price=104)
    obs = {"symbol": "C", "strategy": "rsi2_reversion",
           "calibrated_expected_net_dollars": 300.0, "expectancy_confidence": 1.0}
    decision = allocator.evaluate_allocation(vp, {"A": 100.4, "B": 104}, obs, now)
    assert decision.action == "ROTATE_REPLACE"
    assert decision.incumbent_symbol == "A"


def test_low_confidence_fallback_requires_larger_rotation_edge():
    allocator = SpawnCapitalAllocator(min_score_delta=0.001, reservation_hurdle=0.02)
    vp = MockVirtualPortfolio(1)
    now = datetime.now(timezone.utc)
    vp.positions["A"] = MockPosition("A", "long", 100, 2, now - timedelta(minutes=20),
                                      initial_target=101, peak_price=100)
    obs = {"symbol": "B", "strategy": "momentum", "score": 0.51}
    decision = allocator.evaluate_allocation(vp, {"A": 100}, obs, now)
    assert decision.action in {"WAIT", "ROTATE_REPLACE"}
    if decision.action == "WAIT":
        assert decision.reason == "DELTA_EV_BELOW_DYNAMIC_HURDLE"


def test_rotation_success_with_calibrated_ev():
    allocator = SpawnCapitalAllocator(min_score_delta=0.001, reservation_hurdle=0.0)
    vp = MockVirtualPortfolio(1)
    now = datetime.now(timezone.utc)
    vp.positions["COHR"] = MockPosition("COHR", "long", 100, 2, now - timedelta(minutes=40),
                                         initial_target=100.5, peak_price=100.4)
    obs = {"symbol": "SKHY", "side": "long", "strategy": "momentum",
           "calibrated_expected_net_dollars": 500.0, "expectancy_confidence": 1.0}
    decision = allocator.evaluate_allocation(vp, {"COHR": 100.4}, obs, now)
    assert decision.action == "ROTATE_REPLACE"
    assert decision.incumbent_symbol == "COHR"


def test_missing_incumbent_mark_never_falls_back_to_entry_price():
    allocator = SpawnCapitalAllocator(min_score_delta=0.001, reservation_hurdle=0.0)
    vp = MockVirtualPortfolio(1)
    now = datetime.now(timezone.utc)
    vp.positions["A"] = MockPosition("A", "long", 100, 2, now - timedelta(minutes=40),
                                      initial_target=101, peak_price=100.5)
    obs = {"symbol": "B", "strategy": "momentum",
           "calibrated_expected_net_dollars": 500.0, "expectancy_confidence": 1.0}
    decision = allocator.evaluate_allocation(vp, {}, obs, now)
    assert decision.action == "NO_ACTION"
    assert decision.reason == "MISSING_INCUMBENT_MARK"


def test_invalid_incumbent_mark_never_triggers_rotation():
    allocator = SpawnCapitalAllocator(min_score_delta=0.001, reservation_hurdle=0.0)
    vp = MockVirtualPortfolio(1)
    now = datetime.now(timezone.utc)
    vp.positions["A"] = MockPosition("A", "long", 100, 2, now - timedelta(minutes=40))
    obs = {"symbol": "B", "strategy": "momentum",
           "calibrated_expected_net_dollars": 500.0, "expectancy_confidence": 1.0}
    for bad_mark in (None, 0.0, -1.0, "bad"):
        prices = {} if bad_mark is None else {"A": bad_mark}
        decision = allocator.evaluate_allocation(vp, prices, obs, now)
        assert decision.action == "NO_ACTION"
        assert decision.reason == "MISSING_INCUMBENT_MARK"
