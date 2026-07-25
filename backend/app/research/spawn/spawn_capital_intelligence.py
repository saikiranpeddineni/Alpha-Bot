"""Causal capital-efficiency intelligence for the SHADOW-ONLY spawn laboratory.

This module never sizes or submits production orders. It derives research metrics exclusively
from already-closed virtual trades, so all state is causal at the evaluation timestamp.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import math


@dataclass(frozen=True)
class CapitalIntelligence:
    state: str
    completed_trades: int
    consecutive_wins: int
    cumulative_r: float
    realized_pnl: float
    capital_minutes: float
    pnl_per_capital_minute: float
    evidence_confidence: float
    profit_reinvestment_multiplier: float


def _duration_minutes(trade: dict) -> float:
    try:
        start = datetime.fromisoformat(str(trade.get("entry_at")))
        end = datetime.fromisoformat(str(trade.get("exit_at")))
        seconds = (end - start).total_seconds()
        return max(1.0 / 60.0, seconds / 60.0) if seconds >= 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def analyze_closed_trades(closed_trades: list[dict], *, min_evidence_trades: int = 5, proven_r: float = 2.0, compound_r: float = 4.0, max_multiplier: float = 1.50) -> CapitalIntelligence:
    """Derive BASE -> PROVEN -> COMPOUND -> PROTECT from realized evidence only."""
    trades = list(closed_trades or [])
    completed = len(trades)
    cumulative_r = sum(float(t.get("realized_r", 0.0) or 0.0) for t in trades)
    realized_pnl = sum(float(t.get("pnl", 0.0) or 0.0) for t in trades)
    durations = [_duration_minutes(t) for t in trades]
    valid_time_evidence = completed == 0 or all(d > 0 for d in durations)
    capital_minutes = sum(abs(float(t.get("entry_notional", 0.0) or 0.0)) * d for t, d in zip(trades, durations)) if valid_time_evidence else 0.0
    pnl_per_capital_minute = realized_pnl / capital_minutes if capital_minutes > 0 else 0.0

    consecutive_wins = 0
    for trade in reversed(trades):
        if float(trade.get("realized_r", 0.0) or 0.0) > 0:
            consecutive_wins += 1
        else:
            break

    confidence = min(1.0, completed / float(max(1, min_evidence_trades))) if valid_time_evidence else 0.0
    had_compound_evidence = False
    running_r = 0.0
    for idx, trade in enumerate(trades):
        running_r += float(trade.get("realized_r", 0.0) or 0.0)
        if idx + 1 >= min_evidence_trades and running_r >= compound_r:
            had_compound_evidence = True

    last_r = float(trades[-1].get("realized_r", 0.0) or 0.0) if trades else 0.0
    if not valid_time_evidence or completed < min_evidence_trades or cumulative_r < proven_r or realized_pnl <= 0:
        state, multiplier = "BASE", 1.0
    elif had_compound_evidence and last_r <= 0:
        state, multiplier = "PROTECT", 1.0
    elif cumulative_r >= compound_r and consecutive_wins >= 2:
        state = "COMPOUND"
        multiplier = min(max_multiplier, 1.0 + 0.10 * min(consecutive_wins, 5))
    else:
        state, multiplier = "PROVEN", 1.0

    return CapitalIntelligence(state, completed, consecutive_wins, cumulative_r, realized_pnl, capital_minutes, pnl_per_capital_minute, confidence, multiplier)


def velocity_score(info: CapitalIntelligence) -> float:
    """Bounded reward for profitable capital-time efficiency; invalid/churning paths get zero."""
    if info.realized_pnl <= 0 or info.pnl_per_capital_minute <= 0 or info.evidence_confidence <= 0:
        return 0.0
    return info.evidence_confidence * math.log1p(info.pnl_per_capital_minute * 10_000.0)
