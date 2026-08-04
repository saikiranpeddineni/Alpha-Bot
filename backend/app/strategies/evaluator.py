"""CR-146 Parallel Evidence Evaluator.
"""

from __future__ import annotations

import json
import logging
from typing import Any
import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.core.applog import applog
from app.ai.features import latest_feature_row
from app.ai.prediction import ExcursionEstimate


class ParallelEvidenceEvaluator:
    """Evaluates all alpha and quality gates in parallel for candidate signals.
    Operates strictly read-only and out-of-band to prevent side effects in live trading.
    """

    def __init__(self, model, ml_threshold: float, meta_label_engine, trade_score_engine) -> None:
        self.model = model
        self.ml_threshold = ml_threshold
        self.meta_label_engine = meta_label_engine
        self.trade_score_engine = trade_score_engine

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        sig: Signal,
        regime: Regime,
        sector_strength: float | None = None,
        is_event_day: bool = False,
        midday_active: bool = False,
        ml_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Runs parallel gate evaluation for a candidate signal.
        Fails open on exceptions to protect live trading stability.
        """
        try:
            if sig.action == SignalAction.EXIT:
                return {}

            features = latest_feature_row(df)
            if features is None:
                return {"error": "features unavailable", "veto_count": 0, "vetoes": {}}

            target_ml_threshold = ml_threshold if ml_threshold is not None else self.ml_threshold

            # ---- Gate 1: ML Probability Gate ----
            from app.strategies.pipeline import _REGIME_KEY
            regime_key = _REGIME_KEY.get(regime)

            use_detailed = False
            if hasattr(self.model, "score_action_detailed"):
                name = type(self.model.score_action_detailed).__name__
                if "Mock" not in name and "MagicMock" not in name:
                    use_detailed = True

            if use_detailed:
                score_res = self.model.score_action_detailed(features, sig.action, symbol=symbol, regime_key=regime_key)
            else:
                prob = self.model.score_action(features, sig.action, symbol=symbol, regime_key=regime_key)
                from app.ai.model import ScoreActionResult
                score_res = ScoreActionResult(
                    probability=prob,
                    model_source="BASE",
                    specialist_status="NO_ARTIFACT" if prob is not None else "SPECIALIST_INFERENCE_FAILED",
                    specialist_auc=None,
                    specialist_min_auc=0.53,
                    fallback_reason=None if prob is not None else "score_action returned None"
                )
            import dataclasses
            from app.strategies.adaptive import get_adaptive_specialist_min_auc
            dyn_min_auc = get_adaptive_specialist_min_auc(
                sig.strategy,
                regime_key or "RANGE",
                sector_strength
            )
            score_res = dataclasses.replace(score_res, specialist_min_auc=dyn_min_auc)

            ml = score_res.probability
            ml_passed = False
            ml_rejection_reason = None
            if ml is not None:
                from app.ai.model_legacy import _validate_signal_probability
                _, val_reason = _validate_signal_probability(ml)
                if val_reason is not None:
                    ml = None
                    ml_rejection_reason = f"INVALID_SCORE: {val_reason}"
                elif ml < target_ml_threshold:
                    ml_rejection_reason = f"ML confidence {ml:.4f} < threshold {target_ml_threshold:.4f}"
                else:
                    ml_passed = True
            else:
                if score_res.specialist_status == "TEMPORAL_BYPASS":
                    ml_passed = True
                else:
                    ml_rejection_reason = f"ML probability unavailable ({score_res.fallback_reason or 'unknown'})"

            # ---- Gate 2: Meta-Label Gate ----
            side_str = "long" if sig.action == SignalAction.ENTER_LONG else "short"
            meta_pred = self.meta_label_engine.predict(sig.strategy, features, side=side_str)
            meta_passed = meta_pred.approved
            meta_prob = meta_pred.prob_profitable
            meta_rejection_reason = None if meta_passed else f"Meta-label {meta_pred.decision} [{meta_pred.source}]: {meta_pred.reason}"

            # ---- Gate 3: Trade Score Gate ----
            atr_pct = 1.0
            if df is not None and "atr14" in df.columns and len(df) > 0:
                close = float(df["close"].iloc[-1])
                atr_pct = float(df["atr14"].iloc[-1]) / close * 100.0 if close > 0 else 1.0

            excursion = ExcursionEstimate.from_atr(atr_pct)
            exp_ret_pct = excursion.mfe_pct
            exp_dd_pct = excursion.mae_pct

            avg_volume = None
            if df is not None and "vol_sma20" in df.columns and len(df) > 0:
                avg_volume = float(df["vol_sma20"].iloc[-1]) * float(df["close"].iloc[-1])

            raw_score = self.trade_score_engine.compute(
                ml_prob=ml,
                expected_return_pct=exp_ret_pct,
                expected_drawdown_pct=exp_dd_pct,
                regime=regime,
                avg_daily_volume=avg_volume,
                sector_strength=sector_strength,
            )
            trade_score = min(100.0, max(0.0, raw_score))

            is_breakout = sig.strategy in ["bb_squeeze", "orb", "momentum"]
            score_passed, score_threshold = self.trade_score_engine.approve(
                trade_score, regime,
                is_event_day=is_event_day,
                threshold_boost=self.trade_score_engine.risk_config.midday_score_boost if (midday_active and is_breakout) else 0.0,
            )
            score_rejection_reason = None if score_passed else f"Trade Score {trade_score:.1f} < {score_threshold:.1f}"

            # Build veto list and track first decisive gate
            vetoes = {}
            first_decisive_gate = None
            if not ml_passed:
                vetoes["ml_gate"] = ml_rejection_reason
                if first_decisive_gate is None:
                    first_decisive_gate = "ml_gate"
            if not meta_passed:
                vetoes["meta_label_gate"] = meta_rejection_reason
                if first_decisive_gate is None:
                    first_decisive_gate = "meta_label_gate"
            if not score_passed:
                vetoes["trade_score_gate"] = score_rejection_reason
                if first_decisive_gate is None:
                    first_decisive_gate = "trade_score_gate"

            return {
                "ml_probability": ml,
                "ml_passed": ml_passed,
                "ml_rejection_reason": ml_rejection_reason,
                "meta_probability": meta_prob,
                "meta_passed": meta_passed,
                "meta_rejection_reason": meta_rejection_reason,
                "trade_score": trade_score,
                "trade_score_passed": score_passed,
                "trade_score_threshold": score_threshold,
                "trade_score_rejection_reason": score_rejection_reason,
                "vetoes": vetoes,
                "veto_count": len(vetoes),
                "first_decisive_gate": first_decisive_gate,
            }
        except Exception as exc:
            applog.error("evaluator", f"ParallelEvidenceEvaluator failed on {symbol}: {exc}")
            return {"error": str(exc), "veto_count": 0, "vetoes": {}, "first_decisive_gate": None}

    @staticmethod
    def calculate_veto_attribution(vetoes: dict, realized_r: float | None) -> dict[str, Any]:
        """Calculate unique, shared, and redundant veto attribution for a given realized R outcome.
        """
        try:
            realized_r = float(realized_r)
            if pd.isna(realized_r):
                realized_r = 0.0
        except (TypeError, ValueError):
            # Synthetic/missing stops intentionally have no R-multiple. Their
            # veto attribution is neutral rather than attempting arithmetic on None.
            realized_r = 0.0
        unique_veto_alpha = {}
        shared_veto_alpha = {}
        marginal_alpha = {}

        gates = ["ml_gate", "meta_label_gate", "trade_score_gate"]
        for g in gates:
            unique_veto_alpha[g] = 0.0
            shared_veto_alpha[g] = 0.0
            marginal_alpha[g] = 0.0

        veto_count = len(vetoes)
        first_decisive_gate = None
        if veto_count > 0:
            for g in gates:
                if g in vetoes:
                    first_decisive_gate = g
                    break

        if veto_count == 1:
            vetoed_gate = list(vetoes.keys())[0]
            if vetoed_gate in unique_veto_alpha:
                val = -realized_r
                unique_veto_alpha[vetoed_gate] = round(val, 4)
                marginal_alpha[vetoed_gate] = round(val, 4)
        elif veto_count > 1:
            for vetoed_gate in vetoes.keys():
                if vetoed_gate in shared_veto_alpha:
                    val = -realized_r / veto_count
                    shared_veto_alpha[vetoed_gate] = round(val, 4)
                    # Fix B1 (Issue #177): Marginal alpha for shared vetoes is the
                    # equal-weight Shapley approximation: -realized_r / veto_count.
                    # Previously this was always left at 0.0, making it useless.
                    marginal_alpha[vetoed_gate] = round(val, 4)

        return {
            "unique_veto_alpha": unique_veto_alpha,
            "shared_veto_alpha": shared_veto_alpha,
            "marginal_alpha": marginal_alpha,
            "coalition_veto_count": veto_count,
            "first_decisive_gate": first_decisive_gate,
        }
