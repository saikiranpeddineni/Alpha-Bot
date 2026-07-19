"""Out-of-sample gate-value reporting from the shadow candidate ledger."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _summary(rows: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    outcomes = [r.get("outcomes", {}).get(horizon) for r in rows]
    outcomes = [o for o in outcomes if isinstance(o, dict)]
    returns = [float(val) if (val := o.get("fwd_return")) is not None else 0.0 for o in outcomes]
    rs = [float(val) if (val := o.get("realized_r")) is not None else 0.0 for o in outcomes]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x < 0]
    gross_loss = abs(sum(losses))
    return {
        "candidates": len(rows),
        "labeled": len(outcomes),
        "win_rate": round(len(wins) / len(rs), 4) if rs else None,
        "avg_forward_return": round(sum(returns) / len(returns), 6) if returns else None,
        "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
        "profit_factor": round(sum(wins) / gross_loss, 4) if gross_loss else None,
    }


def build_gate_value_report(
    shadow_signals: list[dict[str, Any]], horizon: str = "15m",
) -> dict[str, Any]:
    """Compare accepted candidates with every rejection gate.

    Outcomes are counterfactual next-bar-open outcomes recorded by ShadowLedger,
    so this report is diagnostic only; it is not a claim of executable P&L.
    """
    accepted = [s for s in shadow_signals if s.get("final_decision") == "accepted"]
    rejected = [s for s in shadow_signals if s.get("final_decision") == "rejected"]
    by_gate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in shadow_signals:
        by_gate[str(signal.get("rejection_stage", "none"))].append(signal)
        key = f"{signal.get('strategy', 'unknown')}:{signal.get('direction', 'unknown')}"
        by_strategy[key].append(signal)

    return {
        "horizon": horizon,
        "methodology": "ShadowLedger next-bar-open counterfactual outcomes; compare only within the same backtest.",
        "all_candidates": _summary(shadow_signals, horizon),
        "accepted": _summary(accepted, horizon),
        "rejected": _summary(rejected, horizon),
        "by_rejection_stage": {name: _summary(rows, horizon) for name, rows in sorted(by_gate.items())},
        "by_strategy_direction": {name: _summary(rows, horizon) for name, rows in sorted(by_strategy.items())},
    }
