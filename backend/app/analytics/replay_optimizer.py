"""Replay Parameter Grid Search Optimizer.

Evaluates combinations of ML confidence thresholds, soft composite thresholds,
and exit policies against historical replayed signals to discover empirical optimal parameters.
"""

from typing import Any
import pandas as pd


class ReplayOptimizer:
    """Evaluates parameter grids over historical replayed signals."""

    def __init__(
        self,
        ml_thresholds: list[float] | None = None,
        exit_policies: list[str] | None = None,
        composite_thresholds: list[float | None] | None = None,
    ) -> None:
        self.ml_thresholds = ml_thresholds or [0.45, 0.48, 0.50, 0.52, 0.55, 0.60]
        self.exit_policies = exit_policies or [
            "exit_production",
            "exit_bracket_tight",
            "exit_bracket_wide",
            "exit_atr_trailing",
            "exit_hybrid_breakeven_atr",
        ]
        self.composite_thresholds = composite_thresholds or [None, 50.0, 55.0, 60.0]

    def optimize(self, signals: list[dict[str, Any]]) -> dict[str, Any]:
        """Run grid evaluation over historical signals."""
        if not signals:
            return {"results": [], "total_signals_evaluated": 0, "best_config": None}

        results = []

        for ml_thresh in self.ml_thresholds:
            for comp_thresh in self.composite_thresholds:
                for policy in self.exit_policies:
                    eval_res = self._evaluate_grid_point(
                        signals=signals,
                        ml_thresh=ml_thresh,
                        comp_thresh=comp_thresh,
                        exit_policy=policy,
                    )
                    results.append(eval_res)

        # Sort by total_realized_r descending
        sorted_results = sorted(results, key=lambda x: x["total_realized_r"], reverse=True)
        best_config = sorted_results[0] if sorted_results else None

        return {
            "total_signals_evaluated": len(signals),
            "best_config": best_config,
            "results": sorted_results,
        }

    def _evaluate_grid_point(
        self,
        signals: list[dict[str, Any]],
        ml_thresh: float,
        comp_thresh: float | None,
        exit_policy: str,
    ) -> dict[str, Any]:
        total_signals = len(signals)
        executed_trades = 0
        avoided_losses_r = 0.0
        missed_winners_r = 0.0

        total_wins = 0
        total_losses = 0
        gross_profit_r = 0.0
        gross_loss_r = 0.0

        total_realized_r = 0.0
        total_mfe_r = 0.0
        total_give_back_r = 0.0
        winner_captures = []

        for s in signals:
            p = s.get("payload") if isinstance(s.get("payload"), dict) else {}
            if not p and isinstance(s.get("payload"), str):
                try:
                    import json
                    p = json.loads(s["payload"])
                except Exception:
                    p = {}

            ml_prob = float(
                s.get("ml_confidence")
                or s.get("ml_probability")
                or s.get("ml_prob")
                or p.get("ml_confidence")
                or p.get("ml_probability")
                or p.get("ml_prob")
                or 0.0
            )
            meta_prob = float(
                s.get("meta_probability")
                or s.get("meta_prob")
                or p.get("meta_probability")
                or p.get("meta_prob")
                or 0.0
            )
            trade_score = float(
                s.get("trade_score")
                or p.get("trade_score")
                or 0.0
            )

            # Determine entry pass/fail under simulated parameters
            ml_pass = ml_prob >= ml_thresh
            if comp_thresh is not None:
                # Soft composite score check
                comp_score = (
                    (min(100.0, max(0.0, ml_prob * 100.0)) * 0.30)
                    + (min(100.0, max(0.0, meta_prob * 100.0)) * 0.25)
                    + (min(100.0, max(0.0, trade_score)) * 0.20)
                    + 25.0  # default baseline for portfolio alignment & risk
                )
                entry_passed = comp_score >= comp_thresh
            else:
                entry_passed = ml_pass

            # Extract counterfactual outcome for exit_policy
            alt_outcomes = (
                s.get("alternative_exits")
                or s.get("outcomes")
                or p.get("alternative_exits")
                or p.get("outcomes")
                or {}
            )
            policy_data = alt_outcomes.get(exit_policy) or {}

            realized_r = policy_data.get("realized_r")
            if realized_r is None:
                # Fallback to simulated_pnl / 100 if R-multiple absent
                sim_pnl = s.get("simulated_pnl")
                if sim_pnl is not None:
                    realized_r = float(sim_pnl) / 100.0

            mfe_r = float(policy_data.get("mfe_r") or 0.0)
            gave_back_r = float(policy_data.get("gave_back_r") or 0.0)
            capture_ratio = float(policy_data.get("winner_capture_ratio") or 0.0)

            if entry_passed:
                executed_trades += 1
                if realized_r is not None:
                    realized_r = float(realized_r)
                    total_realized_r += realized_r
                    total_mfe_r += mfe_r
                    total_give_back_r += gave_back_r

                    if capture_ratio > 0:
                        winner_captures.append(capture_ratio)

                    if realized_r > 0:
                        total_wins += 1
                        gross_profit_r += realized_r
                    elif realized_r < 0:
                        total_losses += 1
                        gross_loss_r += abs(realized_r)
            else:
                # Signal was vetoed under these parameters
                if realized_r is not None:
                    realized_r = float(realized_r)
                    if realized_r < 0:
                        avoided_losses_r += abs(realized_r)
                    elif realized_r > 0:
                        missed_winners_r += realized_r

        win_rate_pct = (total_wins / executed_trades * 100.0) if executed_trades > 0 else 0.0
        profit_factor = (gross_profit_r / gross_loss_r) if gross_loss_r > 0 else (gross_profit_r if gross_profit_r > 0 else 1.0)
        net_gate_alpha_r = avoided_losses_r - missed_winners_r
        avg_realized_r = (total_realized_r / executed_trades) if executed_trades > 0 else 0.0
        avg_give_back_r = (total_give_back_r / executed_trades) if executed_trades > 0 else 0.0
        avg_winner_capture = (sum(winner_captures) / len(winner_captures)) if winner_captures else 0.0

        return {
            "ml_threshold": ml_thresh,
            "composite_threshold": comp_thresh,
            "exit_policy": exit_policy,
            "total_signals": total_signals,
            "executed_trades": executed_trades,
            "win_rate_pct": round(win_rate_pct, 2),
            "profit_factor": round(profit_factor, 2),
            "total_realized_r": round(total_realized_r, 4),
            "avg_realized_r": round(avg_realized_r, 4),
            "avoided_losses_r": round(avoided_losses_r, 4),
            "missed_winners_r": round(missed_winners_r, 4),
            "net_gate_alpha_r": round(net_gate_alpha_r, 4),
            "avg_give_back_r": round(avg_give_back_r, 4),
            "avg_winner_capture_ratio": round(avg_winner_capture, 4),
        }
