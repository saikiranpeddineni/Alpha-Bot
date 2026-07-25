"""Unified Capital-Efficiency Allocator for Spawn Virtual Portfolio (Spawn Allocator V2).

Refactored to resolve all dimensional consistency, sunk-cost, and feasibility requirements:
1. True Dimensionally Consistent Net Dollar Velocity:
   Score = (Expected_Remaining_Net_Dollars / (Capital_Dollars * Remaining_Holding_Hours)) 
           - Strategy_Concentration_Penalty - Friction_Cost

2. Sunk-Cost Elimination: Evaluates expected REMAINING future net dollars from current price 
   to target rather than historical accumulated R. Sunk profit is excluded.

3. Explicit Feasibility Check with Incumbent Release:
   can_open_after_releasing(virtual_portfolio, candidate_symbol, incumbent_symbol)
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List
from app.core.logging import applog


@dataclass
class IncumbentScore:
    symbol: str
    side: str
    entry_price: float
    current_price: float
    remaining_expected_dollars: float
    holding_mins: float
    capital_dollars: float
    profile_quality: float
    net_dollar_velocity: float  # $ / capital-hour
    score: float


@dataclass
class CandidateScore:
    symbol: str
    side: str
    strategy: str
    expected_net_dollars: float
    capital_dollars: float
    profile_quality: float
    friction_cost_dollars: float
    net_dollar_velocity: float  # $ / capital-hour
    score: float


@dataclass
class AllocationDecision:
    action: str  # "SPAWN_NEW", "ROTATE_REPLACE", "NO_ACTION", "INFEASIBLE"
    candidate_symbol: str
    score_delta: float
    incumbent_symbol: Optional[str] = None
    incumbent_score: Optional[float] = None
    candidate_score: float = 0.0
    reason: str = ""


class SpawnCapitalAllocator:
    """Production-grade capital-efficiency allocator for Spawn Army sidecar (V2)."""

    def __init__(
        self,
        enabled: bool = True,
        min_score_delta: float = 0.002,                       # min net dollar-velocity edge ($/cap-hour)
        alpha_stagnation_decay: float = 0.001,                # decay per stagnant minute > 15m
        beta_strategy_concentration_penalty: float = 0.005,   # penalty if strategy family matches existing
        gamma_friction_cost_pct: float = 0.001,               # 0.1% round-trip friction cost
        min_incumbent_age_mins: float = 15.0,                 # incumbent must be held >= 15m before rotation
        default_holding_mins: float = 30.0,                   # baseline expected hold time
    ) -> None:
        self.enabled = enabled
        self.min_score_delta = min_score_delta
        self.alpha_stagnation_decay = alpha_stagnation_decay
        self.beta_strategy_concentration_penalty = beta_strategy_concentration_penalty
        self.gamma_friction_cost_pct = gamma_friction_cost_pct
        self.min_incumbent_age_mins = min_incumbent_age_mins
        self.default_holding_mins = default_holding_mins

    def can_open_after_releasing(
        self,
        virtual_portfolio: Any,
        candidate_symbol: str,
        incumbent_symbol: Optional[str] = None
    ) -> bool:
        """Verifies if candidate can open in virtual_portfolio after releasing incumbent."""
        if not candidate_symbol or not virtual_portfolio:
            return False
        
        cand_upper = candidate_symbol.upper()
        positions = getattr(virtual_portfolio, "positions", {})
        
        # Cannot open if candidate symbol is already held
        if cand_upper in positions:
            return False
            
        max_pos = getattr(virtual_portfolio, "max_open_positions", 2)
        open_count = len(positions)
        
        if incumbent_symbol:
            inc_upper = incumbent_symbol.upper()
            if inc_upper in positions:
                open_count -= 1  # Releasing incumbent frees 1 position slot
                
        return open_count < max_pos

    def check_feasibility(
        self,
        virtual_portfolio: Any,
        candidate_symbol: str,
        incumbent_symbol: Optional[str] = None
    ) -> bool:
        """Explicit Feasibility Pre-Filter."""
        return self.can_open_after_releasing(virtual_portfolio, candidate_symbol, incumbent_symbol)

    def evaluate_incumbent(
        self,
        position: Any,
        current_price: float,
        now: datetime,
        profiles: Optional[Dict[str, Any]] = None
    ) -> IncumbentScore:
        entry = getattr(position, "entry_price", current_price)
        side = str(getattr(position, "side", "long")).lower()
        qty = float(getattr(position, "qty", 1.0) or 1.0)
        entry_at = getattr(position, "entry_at", now)
        target = getattr(position, "initial_target", None)
        
        holding_mins = max(1.0, (now - entry_at).total_seconds() / 60.0)
        capital_dollars = max(1.0, entry * qty)
        
        # Expected REMAINING Future Value From Current Price (Forward-Looking, Sunk Profit Excluded)
        if target and target > 0:
            if side == "long":
                remaining_per_share = max(0.01, target - current_price)
            else:
                remaining_per_share = max(0.01, current_price - target)
            remaining_expected_dollars = remaining_per_share * qty
        else:
            remaining_expected_dollars = capital_dollars * 0.015  # Fallback 1.5% target move

        prof_q = 0.0
        if profiles:
            sym_p = profiles.get(f"SYMBOL:{position.symbol.upper()}")
            if sym_p and getattr(sym_p, "trades", 0) >= 5:
                prof_q += getattr(sym_p, "quality", 0.0)

        # Baseline expected remaining hold hours
        expected_hold_hours = max(0.1, self.default_holding_mins / 60.0)
        
        # True Dimensionally Consistent Dollar Velocity ($ / capital-hour)
        net_dollar_velocity = remaining_expected_dollars / (capital_dollars * expected_hold_hours)
        
        # Stagnation Decay subtracts for stagnant positions held > 15m
        stagnant_mins = max(0.0, holding_mins - 15.0)
        stagnant_decay = self.alpha_stagnation_decay * stagnant_mins
        
        score = net_dollar_velocity + 0.005 * prof_q - stagnant_decay

        return IncumbentScore(
            symbol=position.symbol.upper(),
            side=side,
            entry_price=entry,
            current_price=current_price,
            remaining_expected_dollars=remaining_expected_dollars,
            holding_mins=holding_mins,
            capital_dollars=capital_dollars,
            profile_quality=prof_q,
            net_dollar_velocity=net_dollar_velocity,
            score=score
        )

    def evaluate_candidate(
        self,
        candidate_obs: Dict[str, Any],
        capital_dollars: float = 10000.0,
        profiles: Optional[Dict[str, Any]] = None,
        existing_strategies: Optional[List[str]] = None
    ) -> CandidateScore:
        symbol = str(candidate_obs.get("symbol", "")).upper()
        side = str(candidate_obs.get("side", "short")).lower()
        strategy = str(candidate_obs.get("strategy", "unknown")).lower()
        
        raw_edge = float(candidate_obs.get("score", candidate_obs.get("edge", 0.50)) or 0.50)
        
        # Calibrated Expected Outcome Mapping: 1.5% target move * calibrated edge multiplier
        calibrated_edge_multiplier = max(0.2, raw_edge / 0.50)
        expected_net_dollars = capital_dollars * 0.015 * calibrated_edge_multiplier
        
        prof_q = 0.0
        if profiles and symbol:
            sym_p = profiles.get(f"SYMBOL:{symbol}")
            if sym_p and getattr(sym_p, "trades", 0) >= 5:
                prof_q += getattr(sym_p, "quality", 0.0)

        # Strategy Concentration Penalty (renamed from correlation)
        concentration_penalty = 0.0
        if existing_strategies and strategy in existing_strategies:
            concentration_penalty = self.beta_strategy_concentration_penalty

        # Round-trip friction cost ($)
        friction_cost_dollars = capital_dollars * self.gamma_friction_cost_pct

        expected_hold_hours = max(0.1, self.default_holding_mins / 60.0)
        
        # True Dimensionally Consistent Dollar Velocity ($ / capital-hour)
        net_dollar_velocity = (expected_net_dollars - friction_cost_dollars) / (capital_dollars * expected_hold_hours)
        
        score = net_dollar_velocity + 0.005 * prof_q - concentration_penalty

        return CandidateScore(
            symbol=symbol,
            side=side,
            strategy=strategy,
            expected_net_dollars=expected_net_dollars,
            capital_dollars=capital_dollars,
            profile_quality=prof_q,
            friction_cost_dollars=friction_cost_dollars,
            net_dollar_velocity=net_dollar_velocity,
            score=score
        )

    def evaluate_allocation(
        self,
        virtual_portfolio: Any,
        last_prices: Dict[str, float],
        candidate_obs: Dict[str, Any],
        now: datetime,
        profiles: Optional[Dict[str, Any]] = None,
        candidate_capital: float = 10000.0
    ) -> AllocationDecision:
        if not self.enabled:
            return AllocationDecision(action="NO_ACTION", candidate_symbol="", score_delta=0.0, reason="ALLOCATOR_DISABLED")

        candidate_symbol = str(candidate_obs.get("symbol", "")).upper()
        if not candidate_symbol:
            return AllocationDecision(action="NO_ACTION", candidate_symbol="", score_delta=0.0, reason="MISSING_CANDIDATE_SYMBOL")

        # 1. Direct Feasibility: Can virtual portfolio open directly without rotation?
        has_free_slot = hasattr(virtual_portfolio, "can_open_position") and virtual_portfolio.can_open_position(candidate_symbol)
        
        existing_strategies = []
        if hasattr(virtual_portfolio, "positions"):
            existing_strategies = [str(getattr(pos, "strategy", "")).lower() for pos in virtual_portfolio.positions.values()]

        cand_eval = self.evaluate_candidate(candidate_obs, candidate_capital, profiles, existing_strategies)

        if has_free_slot:
            return AllocationDecision(
                action="SPAWN_NEW",
                candidate_symbol=candidate_symbol,
                score_delta=cand_eval.score,
                candidate_score=cand_eval.score,
                reason="FREE_SLOT_AVAILABLE"
            )

        # 2. Portfolio Full -> Evaluate Feasibility with Incumbent Release
        if not hasattr(virtual_portfolio, "positions") or not virtual_portfolio.positions:
            return AllocationDecision(
                action="INFEASIBLE",
                candidate_symbol=candidate_symbol,
                score_delta=0.0,
                candidate_score=cand_eval.score,
                reason="PORTFOLIO_FULL_NO_INCUMBENTS"
            )

        incumbent_evals: List[Tuple[Any, IncumbentScore]] = []
        for sym, pos in virtual_portfolio.positions.items():
            px = float(last_prices.get(sym, getattr(pos, "entry_price", 100.0)))
            inc_eval = self.evaluate_incumbent(pos, px, now, profiles)
            incumbent_evals.append((pos, inc_eval))

        # Sort incumbents by expected dollar-velocity score (weakest forward velocity first)
        incumbent_evals.sort(key=lambda item: item[1].score)
        weakest_pos, weakest_eval = incumbent_evals[0]

        # Feasibility check: Can candidate open IF weakest incumbent is released?
        if not self.can_open_after_releasing(virtual_portfolio, candidate_symbol, weakest_eval.symbol):
            return AllocationDecision(
                action="INFEASIBLE",
                candidate_symbol=candidate_symbol,
                score_delta=0.0,
                incumbent_symbol=weakest_eval.symbol,
                incumbent_score=weakest_eval.score,
                candidate_score=cand_eval.score,
                reason="CANNOT_OPEN_AFTER_RELEASING_INCUMBENT"
            )

        # Incumbent Minimum Age Guard
        if weakest_eval.holding_mins < self.min_incumbent_age_mins:
            return AllocationDecision(
                action="NO_ACTION",
                candidate_symbol=candidate_symbol,
                score_delta=cand_eval.score - weakest_eval.score,
                incumbent_symbol=weakest_eval.symbol,
                incumbent_score=weakest_eval.score,
                candidate_score=cand_eval.score,
                reason=f"INCUMBENT_TOO_YOUNG_{weakest_eval.holding_mins:.1f}M"
            )

        score_delta = cand_eval.score - weakest_eval.score

        if score_delta >= self.min_score_delta:
            return AllocationDecision(
                action="ROTATE_REPLACE",
                candidate_symbol=candidate_symbol,
                score_delta=score_delta,
                incumbent_symbol=weakest_eval.symbol,
                incumbent_score=weakest_eval.score,
                candidate_score=cand_eval.score,
                reason="ROTATION_DOLLAR_VELOCITY_EDGE"
            )

        return AllocationDecision(
            action="NO_ACTION",
            candidate_symbol=candidate_symbol,
            score_delta=score_delta,
            incumbent_symbol=weakest_eval.symbol,
            incumbent_score=weakest_eval.score,
            candidate_score=cand_eval.score,
            reason="SCORE_DELTA_BELOW_MIN"
        )
