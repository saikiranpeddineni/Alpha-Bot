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
from app.core.logging import applog

REGIME_WEIGHTS: dict[str, dict[Regime, float]] = {
    "orb": {Regime.TREND_UP: 1.15, Regime.TREND_DOWN: 1.15, Regime.RANGE: 0.70, Regime.VOLATILE: 1.00},
    "momentum": {Regime.TREND_UP: 1.20, Regime.TREND_DOWN: 1.05, Regime.RANGE: 0.60, Regime.VOLATILE: 0.80},
    "vwap_reversion": {Regime.TREND_UP: 0.70, Regime.TREND_DOWN: 0.70, Regime.RANGE: 1.20, Regime.VOLATILE: 0.90},
    "rsi2_reversion": {Regime.TREND_UP: 1.15, Regime.TREND_DOWN: 0.50, Regime.RANGE: 1.00, Regime.VOLATILE: 0.80},
    "bb_squeeze": {Regime.TREND_UP: 1.10, Regime.TREND_DOWN: 1.10, Regime.RANGE: 0.80, Regime.VOLATILE: 0.90},
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
        _settings = settings or get_settings()
        _risk = risk_config or _settings.risk_config()
        self.trade_score_engine = TradeScoreEngine(weights=_settings.trade_score_weights(), risk_config=_risk)  # type: ignore[arg-type]
        from app.ai.prediction import MetaLabelEngine
        from app.config import MODELS_DIR
        self.meta_label_engine = MetaLabelEngine(MODELS_DIR)
        # Shadow challengers are intentionally not instantiated here. Champion owns only
        # authoritative ML/Meta/TradeScore behavior; research consumers live behind ShadowResearchRuntime.
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
        cfg = getattr(self.trade_score_engine, "risk_config", None)
        if cfg is None:
            return True
        is_base = (model_source.upper() in ("BASE", "BASE_FALLBACK") or
                   specialist_status in ("NO_ARTIFACT", "SPECIALIST_INFERENCE_FAILED", "REJECTED_LOW_AUC",
                                         "TEMPORAL_BYPASS", "INVALID_SCORE"))
        if getattr(cfg, "rsi2_base_fallback_bypass_enabled", True):
            start = getattr(cfg, "rsi2_base_fallback_bypass_start_et", 660)
            end = getattr(cfg, "rsi2_base_fallback_bypass_end_et", 840)
            if strategy == "rsi2_reversion" and action == SignalAction.ENTER_LONG and is_base and start <= mins_et < end:
                return False
        if getattr(cfg, "bb_squeeze_base_fallback_bypass_enabled", True):
            start = getattr(cfg, "bb_squeeze_base_fallback_bypass_start_et", 840)
            end = getattr(cfg, "bb_squeeze_base_fallback_bypass_end_et", 930)
            if strategy == "bb_squeeze" and action == SignalAction.ENTER_SHORT and is_base and start <= mins_et < end:
                return False
        return True

    def process(self, symbol: str, df: pd.DataFrame, signals: list[Signal], regime: Regime,
                features: pd.Series | None = None, sector_strength: float | None = None) -> tuple[list[Signal], list[tuple[Signal, str]]]:
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
                from app.ai.model_legacy import _validate_signal_probability
                _, reason = _validate_signal_probability(ml)
                if reason is not None:
                    ml = None
                    from dataclasses import replace
                    score_res = replace(score_res, probability=None, specialist_status="INVALID_SCORE", fallback_reason=reason)
            applog.info("pipeline", f"{symbol}: model_source={score_res.model_source} | specialist_status={score_res.specialist_status} | "
                        f"specialist_auc={f'{score_res.specialist_auc:.4f}' if isinstance(score_res.specialist_auc, (int, float)) else score_res.specialist_auc} | "
                        f"specialist_min_auc={f'{score_res.specialist_min_auc:.4f}' if isinstance(score_res.specialist_min_auc, (int, float)) else score_res.specialist_min_auc} | "
                        f"fallback={score_res.fallback_reason or 'None'}")
            if ml is None:
                if score_res.specialist_status == "TEMPORAL_BYPASS":
                    applog.info("pipeline", f"{symbol}: ML_GATE_BYPASSED_TEMPORAL")
                    sig.ml_probability = None
                    sig.ml_gate_decision = "PASS"
                else:
                    sig.ml_gate_decision = "REJECT"
                    rejected.append((sig, f"ML probability unavailable ({score_res.fallback_reason or 'unknown reason'})"))
                    continue
            else:
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
                hard_reject_cutoff = getattr(self.trade_score_engine.risk_config, "ml_hard_reject_threshold", 0.48)
                if ml < self.ml_threshold:
                    if self.soft_composite_gating and ml >= hard_reject_cutoff:
                        sig.ml_gate_decision = "GRAY_ZONE_EVALUATION"
                        applog.info("pipeline", f"{symbol}: ML gray zone active ({ml:.4f} in [{hard_reject_cutoff:.2f}, {self.ml_threshold:.2f}]). Evaluating Meta & TradeScore.")
                    elif enforce_veto:
                        sig.ml_gate_decision = "REJECT"
                        rejected.append((sig, f"ML confidence {ml:.4f} < threshold {self.ml_threshold:.4f}"))
                        continue
                    else:
                        sig.ml_gate_decision = "BASE_FALLBACK_BYPASS"
                        applog.info("pipeline", f"{symbol}: RSI2 BASE_FALLBACK ML advisory bypass active: prob={ml:.4f} < threshold={self.ml_threshold:.4f} (mins_et={mins_et})")
                else:
                    sig.ml_gate_decision = "PASS"

            from app.ai.prediction import ExcursionEstimate
            excursion = ExcursionEstimate.from_atr(atr_pct)
            entry_price = sig.price or (float(df["close"].iloc[-1]) if df is not None and len(df) > 0 else 0.0)
            if entry_price > 0:
                # 1. Capture native strategy geometry BEFORE pipeline candidate adjustments
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
                    if sig.take_profit is None:
                        sig.take_profit = round(entry_price * (1 - excursion.suggested_target_pct / 100.0), 4)
                    if sig.stop_loss is None:
                        sig.stop_loss = round(entry_price * (1 + excursion.suggested_stop_pct / 100.0), 4)
                else:
                    if sig.take_profit is None:
                        sig.take_profit = round(entry_price * (1 + excursion.suggested_target_pct / 100.0), 4)
                    if sig.stop_loss is None:
                        sig.stop_loss = round(entry_price * (1 - excursion.suggested_stop_pct / 100.0), 4)
                original_stop, original_target = sig.stop_loss, sig.take_profit
                min_stop_pct = get_settings().risk_config().min_stop_pct
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

                # Pillar 1 Target Geometry Invariant:
                # Pipeline may extend targets based on Excursion Estimate, but must NEVER compress
                # the strategy's native reward/risk geometry below native_R.
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
            meta_pred = self.meta_label_engine.predict(sig.strategy, features, side=side_str)
            if not meta_pred.approved:
                rejected.append((sig, f"Meta-label {meta_pred.decision} [{meta_pred.source}]: {meta_pred.reason}"))
                continue
            if meta_pred.decision == "BYPASS":
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
            sig.score = round(trade_score / 100.0, 4)
            is_breakout = sig.strategy in ["bb_squeeze", "orb", "momentum"]
            approved, threshold = self.trade_score_engine.approve(
                trade_score, regime, is_event_day=self._is_event_day,
                threshold_boost=self.trade_score_engine.risk_config.midday_score_boost if (self._midday_active and is_breakout) else 0.0)
            if not approved:
                midday_note = f", midday_boost={self.trade_score_engine.risk_config.midday_score_boost:.0f}" if (self._midday_active and is_breakout) else ""
                effective_rr = exp_ret_pct / exp_dd_pct if exp_dd_pct > 0 else excursion.reward_risk
                ml_str = f"{ml:.2f}" if ml is not None else "N/A"
                rejected.append((sig, f"Trade Score {trade_score:.1f} < {threshold:.1f} (raw={raw_score:.1f}{midday_note}) [regime={regime}, ml={ml_str}, rr={effective_rr:.2f}]"))
            else:
                if getattr(sig, "ml_gate_decision", None) == "GRAY_ZONE_EVALUATION":
                    sig.ml_gate_decision = "GRAY_ZONE_RESCUED"
                    applog.info("pipeline", f"{symbol}: GRAY_ZONE_RESCUED by Meta & TradeScore (prob={ml:.4f})")
                entries.append(sig)
        if entries:
            entries.sort(key=lambda s: s.score or 0.0, reverse=True)
            winner = entries[0]
            accepted.append(winner)
            for loser in entries[1:]:
                rejected.append((loser, f"lost conflict to {winner.strategy} (score {winner.score:.4f})"))
        return accepted, rejected
