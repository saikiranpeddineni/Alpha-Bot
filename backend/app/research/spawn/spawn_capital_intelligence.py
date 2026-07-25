"""Causal capital-efficiency intelligence for the SHADOW-ONLY spawn laboratory.

This module never sizes or submits production orders.  It derives research metrics exclusively
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
        return max(1.0 / 60.0, (end - start).total_seconds() / 60.0)
    except (TypeError, ValueError):
        return 0.0


def analyze_closed_trades(
    closed_trades: list[dict],
    *,
    min_evidence_trades: int = 5,
    proven_r: float = 2.0,
    compound_r: float = 4.0,
    max_multiplier: float = 1.50,
) -> CapitalIntelligence:
    """Derive a BASE -> PROVEN -> COMPOUND -> PROTECT state from realized evidence only.

    PROTECT is entered after a loss once compounding evidence had previously been achieved.
    The multiplier is deliberately bounded and represents permission to recycle realized profit;
    it is not leverage and must never be applied to seed capital itself.
    """
    trades = list(closed_trades or [])
    completed = len(trades)
    cumulative_r = sum(float(t.get("realized_r", 0.0) or 0.0) for t in trades)
    realized_pnl = sum(float(t.get("pnl", 0.0) or 0.0) for t in trades)
    capital_minutes = sum(abs(float(t.get("entry_notional", 0.0) or 0.0)) * _duration_minutes(t) for t in trades)
    pnl_per_capital_minute = realized_pnl / max(capital_minutes, 1.0)

    consecutive_wins = 0
    for trade in reversed(trades):
        if float(trade.get("realized_r", 0.0) or 0.0) > 0:
            consecutive_wins += 1
        else:
            break

    confidence = min(1.0, completed / float(max(1, min_evidence_trades)))
    had_compound_evidence = False
    running_r = 0.0
    for idx, trade in enumerate(trades):
        running_r += float(trade.get("realized_r", 0.0) or 0.0)
        if idx + 1 >= min_evidence_trades and running_r >= compound_r:
            had_compound_evidence = True

    last_r = float(trades[-1].get("realized_r", 0.0) or 0.0) if trades else 0.0
    if completed < min_evidence_trades or cumulative_r < proven_r or realized_pnl <= 0:
        state = "BASE"
        multiplier = 1.0
    elif had_compound_evidence and last_r <= 0:
        state = "PROTECT"
        multiplier = 1.0
    elif cumulative_r >= compound_r and consecutive_wins >= 2:
        state = "COMPOUND"
        # Evidence unlocks only a bounded multiplier. The portfolio layer still determines the
        # actual amount of realized profit available for reinvestment.
        multiplier = min(max_multiplier, 1.0 + 0.10 * min(consecutive_wins, 5))
    else:
        state = "PROVEN"
        multiplier = 1.0

    return CapitalIntelligence(
        state=state,
        completed_trades=completed,
        consecutive_wins=consecutive_wins,
        cumulative_r=cumulative_r,
        realized_pnl=realized_pnl,
        capital_minutes=capital_minutes,
        pnl_per_capital_minute=pnl_per_capital_minute,
        evidence_confidence=confidence,
        profit_reinvestment_multiplier=multiplier,
    )


def velocity_score(info: CapitalIntelligence) -> float:
    """Bounded reward for profitable capital-time efficiency; churning cannot dominate."""
    if info.realized_pnl <= 0 or info.pnl_per_capital_minute <= 0:
        return 0.0
    return info.evidence_confidence * math.log1p(info.pnl_per_capital_minute * 10_000.0)
