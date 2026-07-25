"""Causal opportunity-specialization research for SHADOW_ONLY spawned policies.

Profiles are built exclusively from already-closed virtual trades. They never grant broker
or production authority and deliberately shrink small samples toward neutral evidence.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from collections import defaultdict
import math


@dataclass(frozen=True)
class OpportunityProfile:
    key: str
    trades: int
    wins: int
    realized_r: float
    pnl: float
    capital_minutes: float
    expectancy_r: float
    win_rate: float
    pnl_per_capital_minute: float
    confidence: float
    quality: float


def _minutes(trade: dict) -> float:
    try:
        start = datetime.fromisoformat(str(trade.get("entry_at")))
        end = datetime.fromisoformat(str(trade.get("exit_at")))
        value = (end - start).total_seconds() / 60.0
        return value if value > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _bucket_hour(value) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
        return f"{dt.hour:02d}"
    except (TypeError, ValueError):
        return "UNKNOWN"


def trade_dimensions(trade: dict) -> tuple[str, ...]:
    side = str(trade.get("side", "UNKNOWN")).upper()
    symbol = str(trade.get("symbol", "UNKNOWN")).upper()
    strategy = str(trade.get("strategy", "UNKNOWN")).upper()
    hour = _bucket_hour(trade.get("entry_at"))
    return (f"SIDE:{side}", f"SYMBOL:{symbol}", f"STRATEGY:{strategy}", f"HOUR:{hour}", f"SIDE_STRATEGY:{side}:{strategy}")


def build_profiles(closed_trades: list[dict], *, evidence_trades: int = 8) -> dict[str, OpportunityProfile]:
    grouped = defaultdict(list)
    for trade in closed_trades or []:
        for key in trade_dimensions(trade):
            grouped[key].append(trade)
    out = {}
    for key, trades in grouped.items():
        n = len(trades)
        wins = sum(float(t.get("realized_r", 0.0) or 0.0) > 0 for t in trades)
        realized_r = sum(float(t.get("realized_r", 0.0) or 0.0) for t in trades)
        pnl = sum(float(t.get("pnl", 0.0) or 0.0) for t in trades)
        cap_minutes = sum(abs(float(t.get("entry_notional", 0.0) or 0.0)) * _minutes(t) for t in trades)
        expectancy = realized_r / n if n else 0.0
        win_rate = wins / n if n else 0.0
        efficiency = pnl / cap_minutes if cap_minutes > 0 else 0.0
        confidence = min(1.0, n / float(max(1, evidence_trades)))
        # Bounded, evidence-shrunk score. Negative expectancy cannot be rescued by turnover.
        edge = max(-2.0, min(2.0, expectancy))
        efficiency_term = math.copysign(math.log1p(abs(efficiency) * 10_000.0), efficiency) if efficiency else 0.0
        quality = confidence * (edge + 0.10 * efficiency_term)
        out[key] = OpportunityProfile(key, n, wins, realized_r, pnl, cap_minutes, expectancy, win_rate, efficiency, confidence, quality)
    return out


def candidate_quality(closed_trades: list[dict], *, side: str, symbol: str, strategy: str, ts: datetime, evidence_trades: int = 8) -> tuple[float, dict[str, OpportunityProfile]]:
    """Return a causal specialization score for a candidate using completed evidence only."""
    profiles = build_profiles(closed_trades, evidence_trades=evidence_trades)
    keys = [f"SIDE:{side.upper()}", f"SYMBOL:{symbol.upper()}", f"STRATEGY:{strategy.upper()}", f"HOUR:{ts.hour:02d}", f"SIDE_STRATEGY:{side.upper()}:{strategy.upper()}"]
    available = [profiles[k] for k in keys if k in profiles and profiles[k].trades >= evidence_trades]
    if not available:
        return 0.0, profiles
    # Average prevents a single tiny/high-return dimension from dominating the decision.
    return sum(p.quality for p in available) / len(available), profiles
