"""Compatibility adapter from the legacy Spawn rotation hook to SpawnCapitalAllocator.

SpawnVirtualPortfolio historically calls check_rotation_opportunity().  Keeping this
adapter avoids a risky portfolio rewrite while ensuring the unified allocator is the
single decision engine used by that path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from app.research.spawn.spawn_allocator import SpawnCapitalAllocator


@dataclass
class IncumbentEvaluation:
    symbol: str
    side: str
    entry_price: float
    current_price: float
    current_r: float
    holding_mins: float
    profile_quality: float
    incumbent_score: float


@dataclass
class IncomingEvaluation:
    symbol: str
    side: str
    candidate_score: float
    profile_quality: float
    incoming_score: float


class _PortfolioView:
    def __init__(self, positions: Dict[str, Any], max_open_positions: int):
        self.positions = positions
        self.max_open_positions = max_open_positions

    def can_open_position(self, symbol: str) -> bool:
        return symbol.upper() not in self.positions and len(self.positions) < self.max_open_positions


def _allocator_from_config(cfg: Dict[str, Any]) -> SpawnCapitalAllocator:
    holding_map = cfg.get("rotation_strategy_holding_mins")
    return SpawnCapitalAllocator(
        enabled=bool(cfg.get("rotation_enabled", False)),
        min_score_delta=float(cfg.get("rotation_min_edge", cfg.get("spawn_min_score_delta", 0.002))),
        alpha_stagnation_decay=float(cfg.get("rotation_stagnation_decay", 0.001)),
        beta_strategy_concentration_penalty=float(cfg.get("rotation_strategy_concentration_penalty", 0.005)),
        gamma_friction_cost_pct=float(cfg.get("rotation_friction_cost_pct", 0.001)),
        min_incumbent_age_mins=float(cfg.get("rotation_min_incumbent_age_mins", 15.0)),
        default_holding_mins=float(cfg.get("rotation_default_holding_mins", 30.0)),
        strategy_holding_mins=holding_map if isinstance(holding_map, dict) else None,
        reservation_hurdle=float(cfg.get("rotation_reservation_hurdle", 0.003)),
        free_slot_hurdle=float(cfg.get("rotation_free_slot_hurdle", 0.0)),
    )


def check_rotation_opportunity(
    positions: Dict[str, Any],
    last_prices: Dict[str, float],
    incoming_observation: Dict[str, Any],
    now: datetime,
    profiles: Optional[Dict[str, Any]] = None,
    risk_config: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[Any, IncumbentEvaluation, IncomingEvaluation, float]]:
    cfg = risk_config or {}
    if not cfg.get("rotation_enabled", False) or not positions:
        return None

    allocator = _allocator_from_config(cfg)
    view = _PortfolioView(positions, int(cfg.get("max_open_positions", len(positions))))
    candidate_capital = float(incoming_observation.get("capital_dollars", cfg.get("rotation_candidate_capital", 10000.0)) or 10000.0)
    decision = allocator.evaluate_allocation(view, last_prices, incoming_observation, now, profiles, candidate_capital)
    if decision.action != "ROTATE_REPLACE" or not decision.incumbent_symbol:
        return None

    incumbent_pos = positions.get(decision.incumbent_symbol)
    if incumbent_pos is None:
        return None
    px = float(last_prices.get(decision.incumbent_symbol, incumbent_pos.entry_price))
    inc = allocator.evaluate_incumbent(incumbent_pos, px, now, profiles)
    cand = allocator.evaluate_candidate(
        incoming_observation,
        candidate_capital,
        profiles,
        [str(getattr(p, "strategy", "unknown")).lower() for p in positions.values()],
    )
    legacy_inc = IncumbentEvaluation(
        symbol=inc.symbol,
        side=inc.side,
        entry_price=inc.entry_price,
        current_price=inc.current_price,
        current_r=inc.progress_r,
        holding_mins=inc.holding_mins,
        profile_quality=inc.profile_quality,
        incumbent_score=inc.score,
    )
    legacy_incoming = IncomingEvaluation(
        symbol=cand.symbol,
        side=cand.side,
        candidate_score=cand.score,
        profile_quality=cand.profile_quality,
        incoming_score=cand.score,
    )
    return incumbent_pos, legacy_inc, legacy_incoming, decision.score_delta
