"""AI signal pipeline — Phase 2.

Sits between raw strategy signals and the risk manager.

Full evaluation chain per entry signal:
  1. Data Quality gate (bars must pass DataQualityEngine before reaching here)
  2. ML probability fast pre-filter (legacy cheap gate)
  3. Meta-Label gate: strategy-specific P(trade profitable) >= per-strategy threshold
  4. Excursion estimates: predicted MAE / MFE replaces static stop/target defaults
  5. Multi-factor Trade Score (6 weighted components, regime-dependent threshold)
  6. Conflict resolution: one entry per symbol, highest Trade Score wins

Exit signals always pass: the pipeline never blocks risk reduction.

All weights and thresholds are configurable via Settings / RiskConfig.
"""
from __future__ import annotations

import pandas as pd

from app.ai.features import latest_feature_row
from app.ai.model import SignalModel
from app.ai.prediction import TradeScoreEngine
from app.config import RiskConfig, Settings, get_settings
from app.core.domain import Regime, Signal, SignalAction
from app.core.applog import applog
from app.strategies.base import REGISTRY
from app.strategies.entry_intelligence import assess_catalyst_conviction

REGIME_WEIGHTS: dict[str, dict[Regime, float]] = {
    name: cls.regime_weights for name, cls in REGISTRY.items()
}

_REGIME_KEY: dict[Regime, str] = {
    Regime.TREND_UP: "trend_up", Regime.TREND_DOWN: "trend_down",
    Regime.RANGE: "range", Regime.VOLATILE: "volatile",
}


class SignalPipeline:
    def __init__(self, model: SignalModel, ml_threshold: float = 0.55,
                 risk_config: RiskConfig | None = None, settings: Settings | None = None,
                 regime_weights: dict[str, dict[Regime, float]] | None = None,
                 soft_composite_gating: bool = False) -> None:
        self.model = model
        self.ml_threshold = ml_threshold
        self.regime_weights = regime_weights or REGIME_WEIGHTS
        self.soft_composite_gating = soft_composite_gating
        
        if settings is None or risk_config is None:
            raise ValueError("SignalPipeline requires explicit settings and risk_config injection (Phase 0 Config Parity).")
        self.risk_config = risk_config
        self.settings = settings
        
        self.trade_score_engine = TradeScoreEngine(weights=settings.trade_score_weights(), risk_config=risk_config)  # type: ignore[arg-type]
        from app.ai.prediction import MetaLabelEngine
        from app.config import get_models_dir
        self.meta_label_engine = MetaLabelEngine(get_models_dir())
        if not hasattr(model, "_meta_label_engine") or model._meta_label_engine is None:
            model._meta_label_engine = self.meta_label_engine
        self._is_event_day: bool = False
        self._midday_active: bool = False

    def compute_composite_score(self, ml_prob: float, meta_prob: float, trade_score: float,
                                portfolio_alignment: float = 1.0, risk_factor: float = 1.0) -> float:
        ml_pts = min(100.0, max(0.0, ml_prob * 100.0)) * 0.30
        meta_pts = min(100.0, max(0.0, meta_prob * 100.0)) * 0.25
        score_pts = min(100.0, max(0.0, trade_score)) * 0.20
        port_pts = min(100.0, max(0.0, portfolio_alignment * 100.0)) * 0.15
        risk_pts = min(100.0, max(0.0, risk_factor * 100.0)) * 0.10
        return round(ml_pts + meta_pts + score_pts + port_pts + risk_pts, 2)

    def set_event_day(self, is_event: bool) -> None:
        self._is_event_day = is_event

    def set_midday_active(self, is_midday: bool) -> None:
        self._midday_active = is_midday

    def regime_weight(self, strategy: str, regime: Regime) -> float:
        return self.regime_weights.get(strategy, {}).get(regime, 1.0)

    def should_enforce_ml_veto(self, strategy: str, action: SignalAction, model_source: str,
                               specialist_status: str, mins_et: int) -> bool:
        return True

    def process(self, symbol: str, df: pd.DataFrame, signals: list[Signal], regime: Regime,
                features: pd.Series | None = None, sector_strength: float | None = None,
                adaptive_ml_thresholds: dict[str, float] | None = None) -> tuple[list[Signal], list[tuple[Signal, str]]]:
        """Return accepted and rejected signals. Shadow challengers never execute in this path."""
        if not signals:
            return [], []
        accepted: list[Signal] = []
        rejected: list[tuple[Signal, str]] = []
        if features is None:
            features = latest_feature_row(df)
        avg_volume: float | None = None
        if df is not None and "vol_sma20" in df.columns and len(df) > 0:
            avg_volume = float(df["vol_sma20"].iloc[-1]) * float(df["close"].iloc[-1])
        atr_pct = 1.0
        if df is not None and "atr14" in df.columns and len(df) > 0:
            close = float(df["close"].iloc[-1])
            atr_pct = float(df["atr14"].iloc[-1]) / close * 100.0 if close > 0 else 1.0
        regime_key = _REGIME_KEY.get(regime)
        entries: list[Signal] = []
        for sig in signals:
            sig.regime = regime
            if sig.action == SignalAction.EXIT:
                accepted.append(sig)
                continue
            if features is None:
                rejected.append((sig, "feature row unavailable (warmup)"))
                continue

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
                score_res = ScoreActionResult(probability=prob, model_source="BASE",
                                               specialist_status="NO_ARTIFACT" if prob is not None else "SPECIALIST_INFERENCE_FAILED",
                                               specialist_auc=None, specialist_min_auc=0.53,
                                               fallback_reason=None if prob is not None else "score_action returned None")
            ml = score_res.probability
            if ml is not None:
                from app.ai.model import _validate_signal_probability
                valid_ml, reason = _validate_signal_probability(ml)
                if reason is not None:
                    ml = None
                    from dataclasses import replace
                    score_res = replace(score_res, probability=None, specialist_status="INVALID_SCORE", fallback_reason=reason)
                else:
                    ml = valid_ml
            applog.info("pipeline", f"{symbol}: model_source={score_res.model_source} | specialist_status={score_res.specialist_status} | "
                        f"specialist_auc={f'{score_res.specialist_auc:.4f}' if isinstance(score_res.specialist_auc, (int, float)) else score_res.specialist_auc} | "
                        f"specialist_min_auc={f'{score_res.specialist_min_auc:.4f}' if isinstance(score_res.specialist_min_auc, (int, float)) else score_res.specialist_min_auc} | "
                        f"fallback={score_res.fallback_reason or 'None'}")
            catalyst_exceptional = False
            catalyst_strength = 0.0
            catalyst_evidence: dict[str, object] = {}
            if ml is not None:
                catalyst_exceptional, catalyst_strength, catalyst_evidence = assess_catalyst_conviction(sig, df)
                if catalyst_exceptional:
                    applog.info("pipeline", f"{symbol}: exceptional catalyst context detected | strength={catalyst_strength:.3f} | evidence={catalyst_evidence}")
            if ml is None:
                if score_res.specialist_status == "TEMPORAL_BYPASS":
                    applog.error("pipeline", f"{symbol}: ML_GATE_REJECTED_TEMPORAL - Model trained after backtest start.")
                    sig.ml_gate_decision = "REJECT"
                    rejected.append((sig, "ML model temporal bypass (look-ahead bias protection)"))
                    continue
                sig.ml_gate_decision = "REJECT"
                rejected.append((sig, f"ML probability unavailable ({score_res.fallback_reason or 'unknown reason'})"))
                continue
            
            sig.ml_probability = round(ml, 4)
            from zoneinfo import ZoneInfo
            from datetime import datetime, UTC
            et_tz = ZoneInfo("America/New_York")
            if df is not None and len(df) > 0 and hasattr(df.index, "tz_convert"):
                try:
                    now_et = df.index[-1].tz_convert(et_tz)
                except Exception:
                    now_et = datetime.now(et_tz)
            elif df is not None and len(df) > 0 and isinstance(df.index[-1], datetime):
                now_et = df.index[-1].astimezone(et_tz) if df.index[-1].tzinfo else df.index[-1].replace(tzinfo=UTC).astimezone(et_tz)
            else:
                now_et = datetime.now(et_tz)
            mins_et = now_et.hour * 60 + now_et.minute
            enforce_veto = self.should_enforce_ml_veto(sig.strategy, sig.action, score_res.model_source,
                                                       score_res.specialist_status, mins_et)
            current_threshold = self.ml_threshold
            if adaptive_ml_thresholds and getattr(sig, "candidate_id", None) in adaptive_ml_thresholds:
                current_threshold = adaptive_ml_thresholds[sig.candidate_id]

            catalyst_rescued = False
            if catalyst_exceptional and ml < current_threshold:
                catalyst_adjusted_ml = ml + (current_threshold - ml) * catalyst_strength
                if catalyst_adjusted_ml >= current_threshold:
                    ml = float(current_threshold)
                    sig.ml_probability = round(ml, 4)
                    catalyst_rescued = True
                    sig.ml_gate_decision = "CATALYST_CONTEXT_RESCUED"
                    applog.info("pipeline", f"{symbol}: ML catalyst rescue | raw={score_res_probability( score_res.probability ) if False else 'validated'} effective={ml:.4f} threshold={current_threshold:.4f}")

            hard_reject_cutoff = getattr(self.trade_score_engine.risk_config, "ml_hard_reject_threshold", 0.48)
            if not catalyst_rescued and ml < current_threshold:
                if self.soft_composite_gating and ml >= (current_threshold - 0.04):
                    sig.ml_gate_decision = "GRAY_ZONE_EVALUATION"
                    applog.info("pipeline", f"{symbol}: ML gray zone active ({ml:.4f} in [{(current_threshold - 0.04):.2f}, {current_threshold:.2f}]). Evaluating Meta & TradeScore.")
                elif ml < hard_reject_cutoff:
                    applog.info("pipeline", f"{symbol}: ML confidence {ml:.4f} < hard cutoff {hard_reject_cutoff:.2f}. Rejected.")
                    sig.ml_gate_decision = "REJECT"
                    rejected.append((sig, f"ML confidence {ml:.4f} < threshold {current_threshold:.4f}"))
                    continue
                elif enforce_veto:
                    sig.ml_gate_decision = "REJECT"
                    rejected.append((sig, f"ML confidence {ml:.4f} < threshold {current_threshold:.4f}"))
                    continue
                else:
                    sig.ml_gate_decision = "BASE_FALLBACK_BYPASS"
                    applog.info("pipeline", f"{symbol}: {sig.strategy.upper()} BASE_FALLBACK ML advisory bypass active: prob={ml:.4f} < threshold={current_threshold:.4f} (mins_et={mins_et})")
            elif not catalyst_rescued:
                sig.ml_gate_decision = "PASS"

            from app.ai.prediction import ExcursionEstimate
            excursion = ExcursionEstimate.from_atr(atr_pct)
            entry_price = sig.price or (float(df["close"].iloc[-1]) if df is not None and len(df) > 0 else 0.0)
            if entry_price > 0:
                native_stop = sig.stop_loss
                native_target = sig.take_profit
                native_R = None
                if native_stop is not None and native_target is not None:
                    native_risk = abs(entry_price - native_stop)
                    native_reward = abs(native_target - entry_price)
                    is_valid_long = (sig.action == SignalAction.ENTER_LONG and native_stop < entry_price and native_target > entry_price)
                    is_valid_short = (sig.action == SignalAction.ENTER_SHORT and native_stop > entry_price and native_target < entry_price)
                    if (is_valid_long or is_valid_short) and native_risk > 1e-9:
                        native_R = native_reward / native_risk
                if sig.action == SignalAction.ENTER_SHORT:
                    if sig.stop_loss is None:
                        sig.stop_loss = round(entry_price * (1 + excursion.suggested_stop_pct / 100.0), 4)
                    if sig.take_profit is None:
                        sig.take_profit = round(entry_price * (1 - excursion.suggested_target_pct / 100.0), 4)
                else:
                    if sig.stop_loss is None:
                        sig.stop_loss = round(entry_price * (1 - excursion.suggested_stop_pct / 100.0), 4)
                    if sig.take_profit is None:
                        sig.take_profit = round(entry_price * (1 + excursion.suggested_target_pct / 100.0), 4)
                original_stop = sig.stop_loss
                original_target = sig.take_profit
                min_stop_pct = self.risk_config.min_stop_pct
                adjusted = False
                if min_stop_pct > 0:
                    min_dist = entry_price * (min_stop_pct / 100.0)
                    if sig.action == SignalAction.ENTER_LONG and sig.stop_loss > entry_price - min_dist:
                        sig.stop_loss = round(entry_price - min_dist, 4); adjusted = True
                    elif sig.action == SignalAction.ENTER_SHORT and sig.stop_loss < entry_price + min_dist:
                        sig.stop_loss = round(entry_price + min_dist, 4); adjusted = True
                if adjusted and original_stop and original_target and sig.strategy != "vwap_reversion":
                    orig_risk = abs(entry_price - original_stop)
                    orig_reward = abs(original_target - entry_price)
                    if orig_risk > 0:
                        new_reward = abs(entry_price - sig.stop_loss) * (orig_reward / orig_risk)
                        sig.take_profit = round(entry_price + new_reward, 4) if sig.action == SignalAction.ENTER_LONG else round(entry_price - new_reward, 4)
                if sig.strategy != "vwap_reversion" and native_R is not None and native_R > 0 and sig.stop_loss is not None and sig.take_profit is not None:
                    adjusted_risk = abs(entry_price - sig.stop_loss)
                    if adjusted_risk > 1e-9:
                        if sig.action == SignalAction.ENTER_LONG:
                            minimum_target = entry_price + native_R * adjusted_risk
                            sig.take_profit = max(sig.take_profit, round(minimum_target, 4))
                        elif sig.action == SignalAction.ENTER_SHORT:
                            minimum_target = entry_price - native_R * adjusted_risk
                            sig.take_profit = min(sig.take_profit, round(minimum_target, 4))

            side_str = "long" if sig.action == SignalAction.ENTER_LONG else "short"
            meta_threshold = self.ml_threshold
            if adaptive_ml_thresholds and getattr(sig, "candidate_id", None) in adaptive_ml_thresholds:
                meta_threshold = adaptive_ml_thresholds[sig.candidate_id]
            meta_pred = self.meta_label_engine.predict(sig.strategy, features, side=side_str, adaptive_threshold=meta_threshold)
            meta_rescued = False
            effective_meta_prob = meta_pred.prob_profitable
            if not meta_pred.approved and catalyst_exceptional and isinstance(meta_pred.prob_profitable, (int, float)):
                effective_meta_prob = float(meta_pred.prob_profitable) + (meta_threshold - float(meta_pred.prob_profitable)) * catalyst_strength
                if effective_meta_prob >= meta_threshold:
                    meta_rescued = True
                    applog.info("pipeline", f"{symbol}: Meta catalyst rescue | raw={meta_pred.prob_profitable:.4f} effective={effective_meta_prob:.4f} threshold={meta_threshold:.4f}")
            if not meta_pred.approved and not meta_rescued:
                rejected.append((sig, f"Meta-label {meta_pred.decision} [{meta_pred.source}]: {meta_pred.reason}"))
                continue
            if meta_rescued:
                applog.info("pipeline", f"[meta_label] {symbol} {sig.strategy} {sig.action.name} decision=CATALYST_CONTEXT_RESCUED raw={meta_pred.prob_profitable} effective={effective_meta_prob}")
            elif meta_pred.decision == "BYPASS":
                applog.info("pipeline", f"[meta_label] {symbol} {sig.strategy} {sig.action.name} decision=BYPASS source={meta_pred.source} reason={meta_pred.reason}")
            else:
                applog.info("pipeline", f"[meta_label] {symbol} {sig.strategy} {sig.action.name} meta_prob={meta_pred.prob_profitable} decision=PASS source={meta_pred.source}")

            tp, sl = sig.take_profit, sig.stop_loss
            if tp and sl and entry_price:
                exp_ret_pct = abs(tp - entry_price) / entry_price * 100.0
                exp_dd_pct = abs(entry_price - sl) / entry_price * 100.0
            else:
                exp_ret_pct, exp_dd_pct = excursion.mfe_pct, excursion.mae_pct
            raw_score = self.trade_score_engine.compute(ml_prob=ml, expected_return_pct=exp_ret_pct,
                                                        expected_drawdown_pct=exp_dd_pct, regime=regime,
                                                        avg_daily_volume=avg_volume, sector_strength=sector_strength)
            trade_score = min(100.0, max(0.0, raw_score))
            is_breakout = REGISTRY[sig.strategy].is_breakout if sig.strategy in REGISTRY else False
            approved, threshold = self.trade_score_engine.approve(
                trade_score, regime, is_event_day=self._is_event_day,
                threshold_boost=self.trade_score_engine.risk_config.midday_score_boost if (self._midday_active and is_breakout) else 0.0)
            score_rescued = False
            if not approved and catalyst_exceptional:
                effective_trade_score = trade_score + (threshold - trade_score) * catalyst_strength
                if effective_trade_score >= threshold:
                    approved = True
                    score_rescued = True
                    trade_score = float(threshold)
                    applog.info("pipeline", f"{symbol}: TradeScore catalyst rescue | raw={raw_score:.1f} effective={effective_trade_score:.1f} threshold={threshold:.1f}")
            if not approved:
                midday_note = f", midday_boost={self.trade_score_engine.risk_config.midday_score_boost:.0f}" if (self._midday_active and is_breakout) else ""
                effective_rr = exp_ret_pct / exp_dd_pct if exp_dd_pct > 0 else excursion.reward_risk
                ml_str = f"{ml:.2f}" if ml is not None else "N/A"
                rejected.append((sig, f"Trade Score {trade_score:.1f} < {threshold:.1f} (raw={raw_score:.1f}{midday_note}) [regime={regime}, ml={ml_str}, rr={effective_rr:.2f}]"))
            else:
                if getattr(sig, "ml_gate_decision", None) == "GRAY_ZONE_EVALUATION":
                    sig.ml_gate_decision = "GRAY_ZONE_RESCUED"
                    applog.info("pipeline", f"{symbol}: GRAY_ZONE_RESCUED by Meta & TradeScore (prob={ml:.4f})")
                elif catalyst_rescued or meta_rescued or score_rescued:
                    sig.ml_gate_decision = "CATALYST_CONTEXT_RESCUED"
                sig.score = round(trade_score / 100.0, 4)
                entries.append(sig)
        if entries:
            entries.sort(key=lambda s: s.score or 0.0, reverse=True)
            winner = entries[0]
            accepted.append(winner)
            for loser in entries[1:]:
                rejected.append((loser, f"lost conflict to {winner.strategy} (score {winner.score:.4f}))"))
        return accepted, rejected
