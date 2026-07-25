"""Opportunity-Cost Rotation Engine for Spawn Virtual Portfolio."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

from app.research.spawn.spawn_opportunity_intelligence import build_profiles

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

def evaluate_incumbent(
    position: Any,
    current_price: float,
    now: datetime,
    profiles: Optional[Dict[str, Any]] = None
) -> IncumbentEvaluation:
    risk = position.initial_risk_per_share
    if position.side == "long":
        current_r = (current_price - position.entry_price) / risk
    else:
        current_r = (position.entry_price - current_price) / risk

    holding_mins = max(0.0, (now - position.entry_at).total_seconds() / 60.0)
    
    prof_q = 0.0
    if profiles:
        sym_p = profiles.get(f"SYMBOL:{position.symbol.upper()}")
        if sym_p and sym_p.trades >= 5:
            prof_q += sym_p.quality

    # Incumbent score rewards current R and profile history, penalizes stagnant duration > 30m
    incumbent_score = current_r + 0.15 * prof_q - 0.005 * max(0.0, holding_mins - 30.0)

    return IncumbentEvaluation(
        symbol=position.symbol.upper(),
        side=position.side.lower(),
        entry_price=position.entry_price,
        current_price=current_price,
        current_r=current_r,
        holding_mins=holding_mins,
        profile_quality=prof_q,
        incumbent_score=incumbent_score,
    )

def evaluate_incoming(
    candidate_observation: Dict[str, Any],
    profiles: Optional[Dict[str, Any]] = None
) -> IncomingEvaluation:
    symbol = str(candidate_observation.get("symbol", "")).upper()
    side = str(candidate_observation.get("side", "short")).lower()
    raw_score = float(candidate_observation.get("score", candidate_observation.get("edge", 0.50)) or 0.50)

    prof_q = 0.0
    if profiles and symbol:
        sym_p = profiles.get(f"SYMBOL:{symbol}")
        if sym_p and sym_p.trades >= 5:
            prof_q += sym_p.quality

    incoming_score = raw_score + 0.15 * prof_q

    return IncomingEvaluation(
        symbol=symbol,
        side=side,
        candidate_score=raw_score,
        profile_quality=prof_q,
        incoming_score=incoming_score,
    )

def check_rotation_opportunity(
    positions: Dict[str, Any],
    last_prices: Dict[str, float],
    incoming_observation: Dict[str, Any],
    now: datetime,
    profiles: Optional[Dict[str, Any]] = None,
    risk_config: Optional[Dict[str, Any]] = None
) -> Optional[Tuple[Any, IncumbentEvaluation, IncomingEvaluation, float]]:
    if not risk_config:
        return None

    enabled = bool(risk_config.get("rotation_enabled", False))
    min_edge = float(risk_config.get("rotation_min_edge", 0.0))
    max_incumbent_q = float(risk_config.get("rotation_max_incumbent_quality", 999.0))

    if not enabled or min_edge <= 0.0 or not positions:
        return None

    incoming_eval = evaluate_incoming(incoming_observation, profiles)
    if incoming_eval.symbol in positions:
        # Cannot rotate into a symbol already held
        return None

    evaluations = []
    for sym, pos in positions.items():
        px = float(last_prices.get(sym, pos.entry_price))
        inc_eval = evaluate_incumbent(pos, px, now, profiles)
        evaluations.append((pos, inc_eval))

    # Find weakest incumbent
    evaluations.sort(key=lambda item: item[1].incumbent_score)
    weakest_pos, weakest_eval = evaluations[0]

    score_delta = incoming_eval.incoming_score - weakest_eval.incumbent_score

    if score_delta >= min_edge and weakest_eval.incumbent_score <= max_incumbent_q:
        return (weakest_pos, weakest_eval, incoming_eval, score_delta)

    return None
