from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from app.core.domain import Signal


class OpportunitySession(StrEnum):
    INTRADAY = "intraday"
    OVERNIGHT = "overnight"


@dataclass(frozen=True)
class OpportunityContext:
    session: OpportunitySession = OpportunitySession.INTRADAY
    estimated_round_trip_cost_bps: float = 0.0
    min_net_edge_bps: float = 0.0
    min_reward_risk: float = 1.0


@dataclass(frozen=True)
class OpportunityCandidate:
    signal: Signal
    gross_edge_bps: float
    net_edge_bps: float
    reward_risk: float
    session: OpportunitySession


def latest_price(frame: pd.DataFrame) -> float:
    if frame is None or frame.empty or "close" not in frame:
        return 0.0
    try:
        return float(frame["close"].iloc[-1])
    except (TypeError, ValueError, IndexError):
        return 0.0
