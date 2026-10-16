from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.core.domain import Signal, SignalAction

from .models import OpportunityCandidate, OpportunityContext, OpportunitySession, latest_price


@dataclass(frozen=True)
class OpportunityEngine:
    """Cost-aware candidate evaluator with no sizing or broker side effects."""

    def evaluate(self, signal: Signal, frame: pd.DataFrame, context: OpportunityContext) -> OpportunityCandidate | None:
        if signal.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            return None
        price = float(signal.price or latest_price(frame) or 0.0)
        stop = float(signal.stop_loss or 0.0)
        target = float(signal.take_profit or 0.0)
        if price <= 0 or stop <= 0 or target <= 0:
            return None
        risk = abs(price - stop)
        reward = abs(target - price)
        if risk <= 0 or reward <= 0:
            return None
        reward_risk = reward / risk
        gross_edge_bps = reward / price * 10_000
        net_edge_bps = gross_edge_bps - max(0.0, float(context.estimated_round_trip_cost_bps))
        if reward_risk < float(context.min_reward_risk) or net_edge_bps < float(context.min_net_edge_bps):
            return None
        return OpportunityCandidate(signal, gross_edge_bps, net_edge_bps, reward_risk, context.session)
