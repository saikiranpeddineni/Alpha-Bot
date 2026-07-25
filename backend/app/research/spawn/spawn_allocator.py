"""Unified Capital-Efficiency Allocator for Spawn Virtual Portfolio (Spawn Army v2).

Computes unified capital-efficiency scores:
    Score = (Expected_Return / Capital_Dollars) - Alpha * HoldingTime - Beta * CorrelationPenalty - Gamma * FrictionCost

Enforces Pre-Proposal Feasibility Check to drop candidates that cannot execute in VirtualPortfolio.
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
    current_r: float
    holding_mins: float
    capital_dollars: float
    profile_quality: float
    holding_penalty: float
    score: float


@dataclass
class CandidateScore:
    symbol: str
    side: str
    strategy: str
    expected_r: float
    capital_dollars: float
    profile_quality: float
    friction_cost_pct: float
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
    """Production-grade capital-efficiency allocator for Spawn Army sidecar."""

    def __init__(
        self,
        enabled: bool = True,
        min_score_delta: float = 0.05,
        alpha_holding_penalty: float = 0.002,  # per minute over 15m
        beta_corr_penalty: float = 0.10,       # same strategy family penalty
        gamma_friction_cost: float = 0.005,    # estimated round-trip transaction friction
        max_incumbent_score_for_rotation: float = 1.2, # do not rotate if incumbent > 1.2R
        min_incumbent_age_mins: float = 15.0, # incumbent must be held >= 15m before rotation
    ) -> None:
        self.enabled = enabled
        self.min_score_delta = min_score_delta
        self.alpha_holding_penalty = alpha_holding_penalty
        self.beta_corr_penalty = beta_corr_penalty
        self.gamma_friction_cost = gamma_friction_cost
        self.max_incumbent_score_for_rotation = max_incumbent_score_for_rotation
        self.min_incumbent_age_mins = min_incumbent_age_mins

    def check_feasibility(self, virtual_portfolio: Any, candidate_symbol: str) -> bool:
        """Pre-Proposal Feasibility Check: Verify if candidate can open BEFORE opportunity scoring."""
        if not candidate_symbol or not virtual_portfolio:
            return False
        
        cand_sym_upper = candidate_symbol.upper()
        
        # Cannot open if symbol already held
        if hasattr(virtual_portfolio, "positions") and cand_sym_upper in virtual_portfolio.positions:
            return False
            
        # Check if VirtualPortfolio has available position slot or capital room
        if hasattr(virtual_portfolio, "can_open_position"):
            return bool(virtual_portfolio.can_open_position(cand_sym_upper))
            
        return True

    def evaluate_incumbent(
        self,
        position: Any,
        current_price: float,
        now: datetime,
        profiles: Optional[Dict[str, Any]] = None
    ) -> IncumbentScore:
        risk = getattr(position, "initial_risk_per_share", 1.0) or 1.0
        entry = getattr(position, "entry_price", current_price)
        side = str(getattr(position, "side", "long")).lower()
        qty = float(getattr(position, "qty", 1.0) or 1.0)
        entry_at = getattr(position, "entry_at", now)
        
        if side == "long":
            current_r = (current_price - entry) / risk
        else:
            current_r = (entry - current_price) / risk

        holding_mins = max(0.0, (now - entry_at).total_seconds() / 60.0)
        capital_dollars = max(1.0, entry * qty)
        
        prof_q = 0.0
        if profiles:
            sym_p = profiles.get(f"SYMBOL:{position.symbol.upper()}")
            if sym_p and getattr(sym_p, "trades", 0) >= 5:
                prof_q += getattr(sym_p, "quality", 0.0)

        # Holding penalty for stagnant positions > 15 mins
        stagnant_mins = max(0.0, holding_mins - 15.0)
        holding_penalty = self.alpha_holding_penalty * stagnant_mins

        # Composite score per dollar of capital
        score = (current_r + 0.10 * prof_q - holding_penalty) / (capital_dollars / 10000.0)

        return IncumbentScore(
            symbol=position.symbol.upper(),
            side=side,
            entry_price=entry,
            current_price=current_price,
            current_r=current_r,
            holding_mins=holding_mins,
            capital_dollars=capital_dollars,
            profile_quality=prof_q,
            holding_penalty=holding_penalty,
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
        expected_r = raw_edge * 2.0  # Scale raw edge to expected R
        
        prof_q = 0.0
        if profiles and symbol:
            sym_p = profiles.get(f"SYMBOL:{symbol}")
            if sym_p and getattr(sym_p, "trades", 0) >= 5:
                prof_q += getattr(sym_p, "quality", 0.0)

        # Correlation penalty if adding to an existing strategy family
        corr_penalty = 0.0
        if existing_strategies and strategy in existing_strategies:
            corr_penalty = self.beta_corr_penalty

        # Friction cost deduction (commissions + spread + slippage)
        friction = self.gamma_friction_cost

        capital_denom = max(1.0, capital_dollars / 10000.0)
        score = (expected_r + 0.10 * prof_q - corr_penalty - friction) / capital_denom

        return CandidateScore(
            symbol=symbol,
            side=side,
            strategy=strategy,
            expected_r=expected_r,
            capital_dollars=capital_dollars,
            profile_quality=prof_q,
            friction_cost_pct=friction,
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

        # 1. Feasibility Check: Can virtual portfolio open without rotation?
        has_free_slot = hasattr(virtual_portfolio, "can_open_position") and virtual_portfolio.can_open_position(candidate_symbol)
        
        existing_strategies = []
        if hasattr(virtual_portfolio, "positions"):
            existing_strategies = [str(getattr(pos, "strategy", "")).lower() for pos in virtual_portfolio.positions.values()]

        cand_eval = self.evaluate_candidate(candidate_obs, candidate_capital, profiles, existing_strategies)

        if has_free_slot:
            # Case A: Free capital/slot available -> Spawn immediately
            return AllocationDecision(
                action="SPAWN_NEW",
                candidate_symbol=candidate_symbol,
                score_delta=cand_eval.score,
                candidate_score=cand_eval.score,
                reason="FREE_SLOT_AVAILABLE"
            )

        # Case B: Portfolio full -> Check Opportunity-Cost Rotation vs Incumbents
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

        # Find weakest incumbent
        incumbent_evals.sort(key=lambda item: item[1].score)
        weakest_pos, weakest_eval = incumbent_evals[0]

        # Rotation Guards
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

        if weakest_eval.current_r > self.max_incumbent_score_for_rotation:
            return AllocationDecision(
                action="NO_ACTION",
                candidate_symbol=candidate_symbol,
                score_delta=cand_eval.score - weakest_eval.score,
                incumbent_symbol=weakest_eval.symbol,
                incumbent_score=weakest_eval.score,
                candidate_score=cand_eval.score,
                reason=f"INCUMBENT_TRENDING_STRONG_{weakest_eval.current_r:.2f}R"
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
                reason="ROTATION_SCORE_DELTA_EXCEEDED"
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
