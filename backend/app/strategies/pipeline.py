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
from app.core.applog import applog
from app.core.domain import Regime, Signal, SignalAction
from app.strategies.base import REGISTRY

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
        _settings = settings or get_settings()
        _risk = risk_config or _settings.risk_config()
        self.trade_score_engine = TradeScoreEngine(weights=_settings.trade_score_weights(), risk_config=_risk)  # type: ignore[arg-type]
        from app.ai.prediction import MetaLabelEngine
        from app.config import get_models_dir
        self.meta_label_engine = MetaLabelEngine(get_models_dir())
        # Shadow challengers are intentionally not instantiated here. Champion owns only
        # authoritative ML/Meta/TradeScore behavior.
        if not hasattr(model, "_meta_label_engine") or model._meta_label_engine is None:
            model._meta_label_engine = self.meta_label_engine
        self._is_event_day: bool = False

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

    def regime_weight(self, strategy: str, regime: Regime) -> float:
        return self.regime_weights.get(strategy, {}).get(regime, 1.0)

    def process(self, symbol: str, df: pd.DataFrame, signals: list[Signal], regime: Regime,
                features: pd.Series | None = None, sector_strength: float | None = None) -> tuple[list[Signal], list[tuple[Signal, str]]]:
        """Return accepted and rejected signals."""
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
            # =============================================================================
            # [MODIFIED 2026-10-14]: CR-08 & L26 - Fail-Soft Warmup Reduced-Confidence Path
            # ORIGINAL INTENT: Unconditionally hard-rejected all signals when feature row is None (< 30 bars),
            # causing 16 high-quality candidate leaks (e.g. COHR +6.5R).
            # REASON FOR CHANGE: When 15 <= len(df) < 30, allow TradeScore-only evaluation with risk_mult 0.5
            # and require strict meta PASS. Signals with < 15 bars remain safely rejected.
            # SAFE ROLLBACK: The original code is preserved below as comments.
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # if features is None:
            #     rejected.append((sig, "feature row unavailable (warmup)"))
            #     continue
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            is_warmup_reduced = False
            if features is None:
                if df is not None and len(df) >= 15:
                    from app.ai.features import compute_features, FEATURE_COLUMNS
                    f_df = compute_features(df)
                    if not f_df.empty and all(col in f_df.columns for col in FEATURE_COLUMNS):
                        features = f_df.iloc[-1][FEATURE_COLUMNS].fillna(0.0).astype(float)
                        is_warmup_reduced = True
                if features is None:
                    rejected.append((sig, "feature row unavailable (warmup < 15 bars)"))
                    continue
            # =============================================================================

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
                # The engine injects the persisted runtime RiskConfig into the
                # pipeline.  Reading process defaults here created a split-brain
                # stop policy after a UI/API risk update.
                min_stop_pct = float(getattr(self.trade_score_engine.risk_config, "min_stop_pct", 0.0))
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

            # Opening blackout: no NEW entries in the first N minutes after the
            # bell (opening-drive noise; measured 100% loss rate 0-5min).
            rc0 = self.trade_score_engine.risk_config
            if (
                getattr(rc0, "enable_opening_blackout", True)
                and df is not None and len(df) > 0 and hasattr(df.index, "tz_convert")
            ):
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    bar_et = df.index[-1].tz_convert(_ZI("America/New_York"))
                    mins_from_open = (bar_et.hour * 60 + bar_et.minute) - (9 * 60 + 30)
                    if 0 <= mins_from_open < int(getattr(rc0, "entry_blackout_minutes_from_open", 10)):
                        sig.ml_gate_decision = "REJECT"
                        rejected.append((sig, f"Opening blackout: entry suppressed ({mins_from_open}min after open)"))
                        continue
                except Exception:
                    pass

            # Entry chase veto: reject candidates whose decision bar was a
            # vertical candle closing at the directional extreme. Evidence:
            # such chases went never-green 77% and lost -49R net (29 sessions).
            # SCOPE: momentum/breakout entries only. Mean-reversion strategies
            # deliberately fade vertical bars -- entering against the extreme
            # is their thesis, so the veto would kill every valid setup.
            rc = self.trade_score_engine.risk_config
            from app.strategies.base import REGISTRY as _REG
            _strat_cls = _REG.get(sig.strategy)
            is_reversion_strat = bool(getattr(_strat_cls, "is_mean_reversion", False)) if _strat_cls else False
            is_structural_breakout = bool(getattr(_strat_cls, "is_structural_breakout", False)) if _strat_cls else False
            if (
                getattr(rc, "enable_entry_chase_veto", True)
                and not is_reversion_strat
                and df is not None and len(df) >= 2
            ):
                last_bar = df.iloc[-1]
                try:
                    b_hi = float(last_bar["high"]); b_lo = float(last_bar["low"])
                    b_cl = float(last_bar["close"]); atr = float(last_bar.get("atr14") or 0.0)
                except (KeyError, TypeError, ValueError):
                    b_hi = b_lo = b_cl = atr = 0.0
                if atr > 0 and b_cl > 0:
                    rng = b_hi - b_lo
                    tr_atr = (rng / atr) if atr > 0 else 0.0
                    close_pos_long = ((b_cl - b_lo) / rng) if rng > 0 else 0.5
                    close_pos_short = ((b_hi - b_cl) / rng) if rng > 0 else 0.5
                    is_long = sig.action == SignalAction.ENTER_LONG
                    ext_pos = close_pos_long if is_long else close_pos_short
                    if (
                        tr_atr >= float(getattr(rc, "entry_chase_tr_atr_mult", 1.8))
                        and ext_pos >= float(getattr(rc, "entry_chase_close_pos", 0.65))
                    ):
                        sig.ml_gate_decision = "REJECT"
                        rejected.append((sig, f"Entry veto: chasing extended signal bar (range {tr_atr:.1f}x ATR, close at {ext_pos*100:.0f}% extreme)"))
                        continue

            # Location veto: price stretched too far from session VWAP in the
            # trade direction = mathematically exhausted entry (reward:risk
            # inverted before the trade even starts).
            if getattr(rc, "enable_entry_location_veto", True) and df is not None and len(df) >= 2:
                last_bar = df.iloc[-1]
                try:
                    b_cl = float(last_bar["close"])
                    vwap_px = float(last_bar.get("vwap") or 0.0)
                    atr_loc = float(last_bar.get("atr14") or 0.0)
                except (KeyError, TypeError, ValueError):
                    b_cl = vwap_px = atr_loc = 0.0
                if vwap_px > 0 and atr_loc > 0 and b_cl > 0:
                    # Only structural breakout strategies (ORB/squeeze release)
                    # are inherently extended from VWAP on valid breakout days.
                    # Continuation strategies such as momentum and gap-and-go
                    # still need the location exhaustion check.
                    from app.strategies.base import REGISTRY as _REG2
                    is_structural_breakout = (
                        sig.strategy in _REG2 and getattr(_REG2[sig.strategy], "is_structural_breakout", False)
                    )
                    if not is_structural_breakout:
                        stretch = ((b_cl - vwap_px) / atr_loc) if sig.action == SignalAction.ENTER_LONG \
                            else ((vwap_px - b_cl) / atr_loc)
                        if stretch > float(getattr(rc, "entry_max_vwap_stretch_atr", 3.0)):
                            sig.ml_gate_decision = "REJECT"
                            rejected.append((sig, f"Entry veto: location exhausted ({stretch:.1f} ATR from VWAP > limit)"))
                            continue

            # Session-extreme proximity guard: momentum entries INTO the
            # session low (shorts) or high (longs) are late continuation
            # chases of a spent multi-bar move -- invisible to the single-bar
            # chase veto. Evidence 2026-08-24: 3/3 gap_and_go shorts within
            # <=2.5 ATR of LOD, all immediate losers (-$230 net). Mean-
            # reversion strategies fade TOWARD extremes by design and are
            # exempt (same scope rule as the chase veto).
            if (
                getattr(rc, "enable_entry_extreme_guard", True)
                and not is_reversion_strat
                and not is_structural_breakout
                and df is not None and len(df) >= 30 and hasattr(df.index, "tz_convert")
            ):
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    _et = _ZI("America/New_York")
                    idx_et = df.index.tz_convert(_et)
                    last_ts = idx_et[-1]
                    sess_mask = (idx_et.date == last_ts.date()) & (
                        (idx_et.hour > 9) | ((idx_et.hour == 9) & (idx_et.minute >= 30))
                    )
                    sess = df.loc[sess_mask]
                    atr_x = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else 0.0
                    # 15 session bars ~= first quarter-hour; the opening
                    # blackout already owns the pre-09:45 window.
                    if len(sess) >= 15 and atr_x > 0:
                        b_cl_x = float(df["close"].iloc[-1])
                        if sig.action == SignalAction.ENTER_SHORT:
                            prox = (b_cl_x - float(sess["low"].min())) / atr_x
                            limit_lbl = "session low"
                        elif sig.action == SignalAction.ENTER_LONG:
                            prox = (float(sess["high"].max()) - b_cl_x) / atr_x
                            limit_lbl = "session high"
                        else:
                            prox = None
                            limit_lbl = ""
                        max_prox = float(getattr(rc, "entry_max_extreme_proximity_atr", 3.0))
                        if prox is not None and prox <= max_prox:
                            sig.ml_gate_decision = "REJECT"
                            rejected.append((sig, f"Entry veto: chasing session extreme ({prox:.1f} ATR from {limit_lbl} <= {max_prox:.1f})"))
                            continue
                except Exception:
                    pass

            # Entry consensus (breakout strategies only): a breakout against the
            # symbol's relative strength vs SPY is a lone-wolf entry -- the
            # market is not corroborating the move. Longs need RS >= threshold,
            # shorts <= threshold. Skipped when RS unavailable (neutral pass).
            if (
                getattr(rc, "enable_entry_consensus", True)
                and _strat_cls is not None and getattr(_strat_cls, "is_structural_breakout", False)
                and sector_strength is not None
            ):
                try:
                    rs_val = float(sector_strength)
                    is_long_c = sig.action == SignalAction.ENTER_LONG
                    if is_long_c and rs_val < float(getattr(rc, "entry_consensus_rs_long_min", 50.0)):
                        sig.ml_gate_decision = "REJECT"
                        rejected.append((sig, f"Entry consensus: long breakout with weak RS ({rs_val:.0f} < {getattr(rc, 'entry_consensus_rs_long_min', 50.0):.0f})"))
                        continue
                    if not is_long_c and rs_val > float(getattr(rc, "entry_consensus_rs_short_max", 50.0)):
                        sig.ml_gate_decision = "REJECT"
                        rejected.append((sig, f"Entry consensus: short breakout with strong RS ({rs_val:.0f} > {getattr(rc, 'entry_consensus_rs_short_max', 50.0):.0f})"))
                        continue
                except (TypeError, ValueError):
                    pass

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
            # =============================================================================
            # [MODIFIED 2026-10-14]: CR-08 & L26 - Warmup Reduced Evaluation Gate
            # ORIGINAL INTENT: Missing ML score or probability < threshold strictly rejected without exception.
            # REASON FOR CHANGE: For warmup reduced evaluation (15 <= len(df) < 30), allow ML prob = threshold,
            # REQUIRE meta PASS, and scale approved trades by warmup_reduced_risk_mult (0.5).
            # SAFE ROLLBACK: The original code is preserved below as comments.
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # if ml is None:
            #     if score_res.specialist_status == "TEMPORAL_BYPASS":
            #         applog.error("pipeline", f"{symbol}: ML_GATE_REJECTED_TEMPORAL - Model trained after backtest start.")
            #         sig.ml_gate_decision = "REJECT"
            #         rejected.append((sig, "ML model temporal bypass (look-ahead bias protection)"))
            #         continue
            #     else:
            #         sig.ml_gate_decision = "REJECT"
            #         rejected.append((sig, f"ML probability unavailable ({score_res.fallback_reason or 'unknown reason'})"))
            #         continue
            # else:
            #     sig.ml_probability = round(ml, 4)
            #     # Strict ML veto: any probability below the configured threshold is
            #     # rejected. All manual bypass hacks have been removed.
            #     current_threshold = self.ml_threshold
            #     if ml < current_threshold:
            #         if self.soft_composite_gating and ml >= (current_threshold - 0.04):
            #             sig.ml_gate_decision = "GRAY_ZONE_EVALUATION"
            #             applog.info("pipeline", f"{symbol}: ML gray zone active ({ml:.4f} in [{(current_threshold - 0.04):.2f}, {current_threshold:.2f}]). Evaluating Meta & TradeScore.")
            #         else:
            #             sig.ml_gate_decision = "REJECT"
            #             rejected.append((sig, f"ML confidence {ml:.4f} < threshold {current_threshold:.4f}"))
            #             continue
            #     else:
            #         sig.ml_gate_decision = "PASS"
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            if is_warmup_reduced:
                ml = float(self.ml_threshold)
                sig.ml_probability = round(ml, 4)
                sig.ml_gate_decision = "WARMUP_REDUCED"
                applog.info("pipeline", f"{symbol}: WARMUP_REDUCED path active (df len < 30 bars, ml={ml:.2f})")
            elif ml is None:
                if score_res.specialist_status == "TEMPORAL_BYPASS":
                    applog.error("pipeline", f"{symbol}: ML_GATE_REJECTED_TEMPORAL - Model trained after backtest start.")
                    sig.ml_gate_decision = "REJECT"
                    rejected.append((sig, "ML model temporal bypass (look-ahead bias protection)"))
                    continue
                else:
                    sig.ml_gate_decision = "REJECT"
                    rejected.append((sig, f"ML probability unavailable ({score_res.fallback_reason or 'unknown reason'})"))
                    continue
            else:
                sig.ml_probability = round(ml, 4)
                current_threshold = self.ml_threshold
                if ml < current_threshold:
                    if self.soft_composite_gating and ml >= (current_threshold - 0.04):
                        sig.ml_gate_decision = "GRAY_ZONE_EVALUATION"
                        applog.info("pipeline", f"{symbol}: ML gray zone active ({ml:.4f} in [{(current_threshold - 0.04):.2f}, {current_threshold:.2f}]). Evaluating Meta & TradeScore.")
                    else:
                        sig.ml_gate_decision = "REJECT"
                        rejected.append((sig, f"ML confidence {ml:.4f} < threshold {current_threshold:.4f}"))
                        continue
                else:
                    sig.ml_gate_decision = "PASS"
            # =============================================================================

            side_str = "long" if sig.action == SignalAction.ENTER_LONG else "short"

            # Keep the global ML floor as a lower bound, but never let it
            # silently weaken a strategy's own validated meta-label threshold.
            thresholds = getattr(self.meta_label_engine, "default_thresholds", {})
            fallback_threshold = getattr(self.meta_label_engine, "FALLBACK_THRESHOLD", 0.5)
            strategy_meta_floor = thresholds.get(sig.strategy, fallback_threshold) if isinstance(thresholds, dict) else fallback_threshold
            if not isinstance(strategy_meta_floor, (int, float)):
                strategy_meta_floor = 0.5
            meta_pred = self.meta_label_engine.predict(
                sig.strategy,
                features,
                side=side_str,
                adaptive_threshold=max(self.ml_threshold, strategy_meta_floor),
            )
            if is_warmup_reduced and meta_pred.decision != "PASS":
                sig.ml_gate_decision = "REJECT"
                rejected.append((sig, f"Warmup reduced evaluation requires meta PASS, got {meta_pred.decision} [{meta_pred.source}]"))
                continue
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
            # Regime weights are tightening-only in the Champion path.  A
            # weight below 1.0 reduces admission in a hostile regime; a weight
            # above 1.0 cannot bypass the configured score threshold.  This
            # preserves the existing capital-protection gates while making the
            # strategy/regime research configuration operational.
            # =============================================================================
            # [MODIFIED 2026-10-14]: CR-07 & CR-09 - Regime Ranking Score & High-Agreement Rescue
            # ORIGINAL INTENT: Regime weight was clamped to min(1.0, weight) for both admission and
            # sig.score; trades with TradeScore < threshold were strictly rejected regardless of ML/Meta agreement.
            # REASON FOR CHANGE:
            # 1. CR-07: Preserve tightening-only effective_regime_weight (<= 1.0) for score admission
            #    threshold, but let configured_regime_weight scale sig.score for candidate conflict ranking.
            # 2. CR-09: High-agreement rescue (ml >= 0.65, meta >= 0.80 PASS) admits high-conviction
            #    trades with risk_mult = 0.75 without lowering the general TradeScore threshold of 70.
            # SAFE ROLLBACK: The original code is preserved below as comments.
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # configured_regime_weight = float(self.regime_weight(sig.strategy, regime))
            # effective_regime_weight = min(1.0, max(0.0, configured_regime_weight))
            # trade_score = min(100.0, max(0.0, raw_score * effective_regime_weight))
            # sig.score = round(trade_score / 100.0, 4)
            # approved, threshold = self.trade_score_engine.approve(trade_score, regime, is_event_day=self._is_event_day)
            # if not approved:
            #     effective_rr = exp_ret_pct / exp_dd_pct if exp_dd_pct > 0 else excursion.reward_risk
            #     ml_str = f"{ml:.2f}" if ml is not None else "N/A"
            #     rejected.append((sig, f"Trade Score {trade_score:.1f} < {threshold:.1f} (raw={raw_score:.1f}, regime_weight={effective_regime_weight:.2f}) [regime={regime}, ml={ml_str}, rr={effective_rr:.2f}]"))
            # else:
            #     if getattr(sig, "ml_gate_decision", None) == "GRAY_ZONE_EVALUATION":
            #         sig.ml_gate_decision = "GRAY_ZONE_RESCUED"
            #         applog.info("pipeline", f"{symbol}: GRAY_ZONE_RESCUED by Meta & TradeScore (prob={ml:.4f})")
            #     entries.append(sig)
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            configured_regime_weight = float(self.regime_weight(sig.strategy, regime))
            effective_regime_weight = min(1.0, max(0.0, configured_regime_weight))
            trade_score = min(100.0, max(0.0, raw_score * effective_regime_weight))
            # CR-07: Candidate ranking reflects configured regime weight for conflict prioritization
            ranking_score = min(100.0, max(0.0, raw_score * configured_regime_weight))
            sig.score = round(ranking_score / 100.0, 4)

            approved, threshold = self.trade_score_engine.approve(trade_score, regime, is_event_day=self._is_event_day)

            # CR-09: High-agreement rescue path
            import os
            rescue_ml_min = float(os.environ.get("RESCUE_ML_MIN", 0.65))
            rescue_meta_min = float(os.environ.get("RESCUE_META_MIN", 0.80))
            rescue_risk_mult = float(os.environ.get("RESCUE_RISK_MULT", 0.75))
            meta_prob = meta_pred.prob_profitable if meta_pred is not None else 0.0
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # is_high_agreement_rescue = (
            #     not approved
            #     and ml is not None
            #     and ml >= rescue_ml_min
            #     and meta_prob >= rescue_meta_min
            #     and getattr(meta_pred, "passed", False)
            # )
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            is_high_agreement_rescue = (
                not approved
                and ml is not None
                and ml >= rescue_ml_min
                and meta_prob >= rescue_meta_min
                and (getattr(meta_pred, "approved", False) or getattr(meta_pred, "decision", "") == "PASS")
            )

            if is_high_agreement_rescue:
                approved = True
                sig.risk_multiplier = min(getattr(sig, "risk_multiplier", 1.0), rescue_risk_mult)
                sig.ml_gate_decision = "HIGH_AGREEMENT_RESCUE"
                applog.info(
                    "pipeline",
                    f"{symbol} {sig.strategy}: HIGH_AGREEMENT_RESCUE (ml={ml:.4f}>={rescue_ml_min}, meta={meta_prob:.4f}>={rescue_meta_min}, ts={trade_score:.1f}<{threshold:.1f}, risk_mult={rescue_risk_mult})",
                )

            if not approved:
                effective_rr = exp_ret_pct / exp_dd_pct if exp_dd_pct > 0 else excursion.reward_risk
                ml_str = f"{ml:.2f}" if ml is not None else "N/A"
                rejected.append((sig, f"Trade Score {trade_score:.1f} < {threshold:.1f} (raw={raw_score:.1f}, regime_weight={effective_regime_weight:.2f}) [regime={regime}, ml={ml_str}, rr={effective_rr:.2f}]"))
            else:
                if is_warmup_reduced:
                    import os
                    warmup_mult = float(os.environ.get("WARMUP_REDUCED_RISK_MULT", 0.5))
                    sig.risk_multiplier = min(getattr(sig, "risk_multiplier", 1.0), warmup_mult)
                    sig.ml_gate_decision = "WARMUP_REDUCED"
                    applog.info("pipeline", f"{symbol} {sig.strategy}: WARMUP_REDUCED approved with risk_mult={sig.risk_multiplier}")
                elif getattr(sig, "ml_gate_decision", None) == "GRAY_ZONE_EVALUATION":
                    sig.ml_gate_decision = "GRAY_ZONE_RESCUED"
                    applog.info("pipeline", f"{symbol}: GRAY_ZONE_RESCUED by Meta & TradeScore (prob={ml:.4f})")
                entries.append(sig)
            # =============================================================================
        if entries:
            entries.sort(key=lambda s: s.score or 0.0, reverse=True)
            winner = entries[0]
            accepted.append(winner)
            for loser in entries[1:]:
                rejected.append((loser, f"lost conflict to {winner.strategy} (score {winner.score:.4f})"))
        return accepted, rejected
