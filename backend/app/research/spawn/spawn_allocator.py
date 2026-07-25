"""Portfolio-aware capital allocator for the Spawn research sidecar.

The allocator deliberately remains broker-isolated.  It ranks scarce capital using
forward value only and exposes calibration hooks so replay/database evidence can
replace fallbacks without changing allocation mechanics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


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
    net_dollar_velocity: float
    score: float
    expected_holding_mins: float = 30.0
    progress_r: float = 0.0
    stagnation_mins: float = 0.0


@dataclass
class CandidateScore:
    symbol: str
    side: str
    strategy: str
    expected_net_dollars: float
    capital_dollars: float
    profile_quality: float
    friction_cost_dollars: float
    net_dollar_velocity: float
    score: float
    expected_holding_mins: float = 30.0
    confidence: float = 0.0


@dataclass
class AllocationDecision:
    action: str  # SPAWN_NEW, ROTATE_REPLACE, WAIT, NO_ACTION, INFEASIBLE
    candidate_symbol: str
    score_delta: float
    incumbent_symbol: Optional[str] = None
    incumbent_score: Optional[float] = None
    candidate_score: float = 0.0
    reason: str = ""


class SpawnCapitalAllocator:
    """Research-only portfolio commander for the Spawn virtual portfolio."""

    DEFAULT_HOLDING_MINS = {
        "rsi2_reversion": 12.0,
        "orb": 18.5,
        "bb_squeeze": 25.0,
        "momentum": 42.0,
    }

    def __init__(
        self,
        enabled: bool = True,
        min_score_delta: float = 0.002,
        alpha_stagnation_decay: float = 0.001,
        beta_strategy_concentration_penalty: float = 0.005,
        gamma_friction_cost_pct: float = 0.001,
        min_incumbent_age_mins: float = 15.0,
        default_holding_mins: float = 30.0,
        strategy_holding_mins: Optional[Dict[str, float]] = None,
        reservation_hurdle: float = 0.003,
        free_slot_hurdle: float = 0.0,
    ) -> None:
        self.enabled = enabled
        self.min_score_delta = min_score_delta
        self.alpha_stagnation_decay = alpha_stagnation_decay
        self.beta_strategy_concentration_penalty = beta_strategy_concentration_penalty
        self.gamma_friction_cost_pct = gamma_friction_cost_pct
        self.min_incumbent_age_mins = min_incumbent_age_mins
        self.default_holding_mins = default_holding_mins
        self.strategy_holding_mins = dict(self.DEFAULT_HOLDING_MINS)
        if strategy_holding_mins:
            self.strategy_holding_mins.update({str(k).lower(): float(v) for k, v in strategy_holding_mins.items()})
        self.reservation_hurdle = max(0.0, reservation_hurdle)
        self.free_slot_hurdle = max(0.0, free_slot_hurdle)

    def _holding_mins(self, strategy: str, observation: Optional[Dict[str, Any]] = None) -> float:
        observation = observation or {}
        explicit = observation.get("expected_holding_mins")
        if explicit is not None:
            return max(1.0, float(explicit))
        return max(1.0, float(self.strategy_holding_mins.get(str(strategy).lower(), self.default_holding_mins)))

    @staticmethod
    def _profile_quality(profiles: Optional[Dict[str, Any]], symbol: str) -> float:
        if not profiles or not symbol:
            return 0.0
        profile = profiles.get(f"SYMBOL:{symbol.upper()}")
        if profile and getattr(profile, "trades", 0) >= 5:
            return float(getattr(profile, "quality", 0.0) or 0.0)
        return 0.0

    def can_open_after_releasing(self, virtual_portfolio: Any, candidate_symbol: str, incumbent_symbol: Optional[str] = None) -> bool:
        if not candidate_symbol or virtual_portfolio is None:
            return False
        positions = getattr(virtual_portfolio, "positions", {})
        candidate = candidate_symbol.upper()
        if candidate in positions:
            return False
        open_count = len(positions)
        if incumbent_symbol and incumbent_symbol.upper() in positions:
            open_count -= 1
        return open_count < int(getattr(virtual_portfolio, "max_open_positions", 2))

    def check_feasibility(self, virtual_portfolio: Any, candidate_symbol: str, incumbent_symbol: Optional[str] = None) -> bool:
        return self.can_open_after_releasing(virtual_portfolio, candidate_symbol, incumbent_symbol)

    def _candidate_expected_dollars(self, obs: Dict[str, Any], capital: float) -> Tuple[float, float]:
        """Prefer causal/calibrated evidence; retain a conservative fallback for old payloads."""
        for key in ("calibrated_expected_net_dollars", "expected_net_dollars", "replay_expected_net_dollars"):
            if obs.get(key) is not None:
                return float(obs[key]), float(obs.get("expectancy_confidence", 1.0) or 1.0)
        if obs.get("expected_return_pct") is not None:
            return capital * float(obs["expected_return_pct"]) / 100.0, float(obs.get("expectancy_confidence", 0.75) or 0.75)

        # Backward-compatible fallback only. It is intentionally bounded because
        # Jul-23/24 scores clustered tightly and must not masquerade as calibrated EV.
        raw = float(obs.get("score", obs.get("edge", 0.50)) or 0.50)
        bounded = min(0.60, max(0.50, raw))
        multiplier = 0.8 + 2.0 * (bounded - 0.50)
        return capital * 0.015 * multiplier, 0.25

    def evaluate_candidate(self, candidate_obs: Dict[str, Any], capital_dollars: float = 10000.0,
                           profiles: Optional[Dict[str, Any]] = None,
                           existing_strategies: Optional[List[str]] = None) -> CandidateScore:
        symbol = str(candidate_obs.get("symbol", "")).upper()
        side = str(candidate_obs.get("side", "long")).lower()
        strategy = str(candidate_obs.get("strategy", "unknown")).lower()
        capital = max(1.0, float(capital_dollars))
        expected_net, confidence = self._candidate_expected_dollars(candidate_obs, capital)
        friction = max(0.0, capital * self.gamma_friction_cost_pct)
        hold_mins = self._holding_mins(strategy, candidate_obs)
        hold_hours = max(0.1, hold_mins / 60.0)
        velocity = (expected_net - friction) / (capital * hold_hours)
        quality = self._profile_quality(profiles, symbol)
        concentration = self.beta_strategy_concentration_penalty if existing_strategies and strategy in existing_strategies else 0.0
        score = velocity + 0.005 * quality - concentration
        return CandidateScore(symbol, side, strategy, expected_net, capital, quality, friction, velocity, score, hold_mins, confidence)

    def evaluate_incumbent(self, position: Any, current_price: float, now: datetime,
                           profiles: Optional[Dict[str, Any]] = None) -> IncumbentScore:
        entry = float(getattr(position, "entry_price", current_price))
        side = str(getattr(position, "side", "long")).lower()
        qty = float(getattr(position, "qty", 1.0) or 1.0)
        entry_at = getattr(position, "entry_at", now)
        target = getattr(position, "initial_target", None)
        strategy = str(getattr(position, "strategy", "unknown") or "unknown").lower()
        holding = max(1.0, (now - entry_at).total_seconds() / 60.0)
        capital = max(1.0, abs(entry * qty))
        risk = max(0.01, float(getattr(position, "initial_risk_per_share", 1.0) or 1.0))
        signed_move = current_price - entry if side == "long" else entry - current_price
        progress_r = signed_move / risk

        if target and float(target) > 0:
            remaining_per_share = (float(target) - current_price) if side == "long" else (current_price - float(target))
            upside = max(0.0, remaining_per_share) * qty
        else:
            upside = capital * 0.015

        hit_prob = getattr(position, "target_hit_probability", None)
        if hit_prob is None:
            hit_prob = min(0.80, max(0.20, 0.50 + 0.10 * progress_r))
        hit_prob = min(1.0, max(0.0, float(hit_prob)))
        downside = max(0.0, -signed_move * qty)
        remaining_ev = hit_prob * upside - (1.0 - hit_prob) * downside

        hold_mins = self._holding_mins(strategy)
        remaining_time = max(6.0, hold_mins - min(holding, hold_mins * 0.8))
        velocity = remaining_ev / (capital * max(0.1, remaining_time / 60.0))
        quality = self._profile_quality(profiles, str(getattr(position, "symbol", "")))

        last_mfe_at = getattr(position, "last_mfe_at", None)
        if isinstance(last_mfe_at, datetime):
            stagnation = max(0.0, (now - last_mfe_at).total_seconds() / 60.0)
        else:
            peak = float(getattr(position, "peak_price", entry) or entry)
            trough = float(getattr(position, "trough_price", entry) or entry)
            favorable = (peak - entry) if side == "long" else (entry - trough)
            near_best = favorable > 0 and abs(favorable - signed_move) <= max(0.05 * risk, 0.01)
            stagnation = 0.0 if near_best else max(0.0, holding - 15.0)
        decay = self.alpha_stagnation_decay * stagnation
        score = velocity + 0.005 * quality - decay
        return IncumbentScore(str(position.symbol).upper(), side, entry, current_price, remaining_ev, holding,
                              capital, quality, velocity, score, hold_mins, progress_r, stagnation)

    def evaluate_allocation(self, virtual_portfolio: Any, last_prices: Dict[str, float], candidate_obs: Dict[str, Any],
                            now: datetime, profiles: Optional[Dict[str, Any]] = None,
                            candidate_capital: float = 10000.0) -> AllocationDecision:
        if not self.enabled:
            return AllocationDecision("NO_ACTION", "", 0.0, reason="ALLOCATOR_DISABLED")
        symbol = str(candidate_obs.get("symbol", "")).upper()
        if not symbol:
            return AllocationDecision("NO_ACTION", "", 0.0, reason="MISSING_CANDIDATE_SYMBOL")

        positions = getattr(virtual_portfolio, "positions", {})
        existing_strategies = [str(getattr(p, "strategy", "unknown")).lower() for p in positions.values()]
        candidate = self.evaluate_candidate(candidate_obs, candidate_capital, profiles, existing_strategies)

        if hasattr(virtual_portfolio, "can_open_position") and virtual_portfolio.can_open_position(symbol):
            remaining_slots = int(getattr(virtual_portfolio, "max_open_positions", 2)) - len(positions)
            hurdle = self.free_slot_hurdle
            if remaining_slots <= 1:
                hurdle = max(hurdle, self.reservation_hurdle)
            if candidate.score < hurdle:
                return AllocationDecision("WAIT", symbol, candidate.score - hurdle, candidate_score=candidate.score,
                                          reason="RESERVE_CAPITAL_FOR_BETTER_OPPORTUNITY")
            return AllocationDecision("SPAWN_NEW", symbol, candidate.score, candidate_score=candidate.score,
                                      reason="FREE_SLOT_EDGE_CLEARS_HURDLE")

        if not positions:
            return AllocationDecision("INFEASIBLE", symbol, 0.0, candidate_score=candidate.score,
                                      reason="PORTFOLIO_FULL_NO_INCUMBENTS")

        # A rotation requires a real current mark for the incumbent. Entry price is not
        # a valid substitute: it can materially distort remaining EV and replacement rank.
        replacement_options: List[Tuple[float, Any, IncumbentScore]] = []
        missing_marks = False
        for sym, pos in positions.items():
            if not self.can_open_after_releasing(virtual_portfolio, symbol, sym):
                continue
            raw_px = last_prices.get(sym)
            if raw_px is None:
                missing_marks = True
                continue
            try:
                px = float(raw_px)
            except (TypeError, ValueError):
                missing_marks = True
                continue
            if px <= 0.0:
                missing_marks = True
                continue
            incumbent = self.evaluate_incumbent(pos, px, now, profiles)
            if incumbent.holding_mins < self.min_incumbent_age_mins:
                continue
            delta = candidate.score - incumbent.score
            replacement_options.append((delta, pos, incumbent))

        if not replacement_options:
            reason = "MISSING_INCUMBENT_MARK" if missing_marks else "NO_LEGAL_MATURE_REPLACEMENT"
            return AllocationDecision("NO_ACTION", symbol, 0.0, candidate_score=candidate.score, reason=reason)

        delta, _, incumbent = max(replacement_options, key=lambda item: item[0])
        uncertainty = (1.0 - min(1.0, max(0.0, candidate.confidence))) * self.reservation_hurdle
        required_delta = self.min_score_delta + uncertainty
        if delta >= required_delta:
            return AllocationDecision("ROTATE_REPLACE", symbol, delta, incumbent.symbol, incumbent.score,
                                      candidate.score, "PORTFOLIO_DELTA_EV_CLEARS_DYNAMIC_HURDLE")
        return AllocationDecision("WAIT", symbol, delta, incumbent.symbol, incumbent.score, candidate.score,
                                  "DELTA_EV_BELOW_DYNAMIC_HURDLE")
