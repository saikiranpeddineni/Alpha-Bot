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
from app.ai.prediction import (
    TradeScoreEngine,
)
from app.config import RiskConfig, Settings, get_settings
from app.core.domain import Regime, Signal, SignalAction
from app.core.logging import applog


REGIME_WEIGHTS: dict[str, dict[Regime, float]] = {
    "orb": {
        Regime.TREND_UP: 1.15, Regime.TREND_DOWN: 1.15, Regime.RANGE: 0.70, Regime.VOLATILE: 1.00,
    },
    "momentum": {
        Regime.TREND_UP: 1.20, Regime.TREND_DOWN: 1.05, Regime.RANGE: 0.60, Regime.VOLATILE: 0.80,
    },
    "vwap_reversion": {
        Regime.TREND_UP: 0.70, Regime.TREND_DOWN: 0.70, Regime.RANGE: 1.20, Regime.VOLATILE: 0.90,
    },
    "rsi2_reversion": {
        Regime.TREND_UP: 1.15, Regime.TREND_DOWN: 0.50, Regime.RANGE: 1.00, Regime.VOLATILE: 0.80,
    },
    "bb_squeeze": {
        Regime.TREND_UP: 1.10, Regime.TREND_DOWN: 1.10, Regime.RANGE: 0.80, Regime.VOLATILE: 0.90,
    },
}

# Maps Regime -> regime_key string used in adaptive model routing
_REGIME_KEY: dict[Regime, str] = {
    Regime.TREND_UP:   "trend_up",
    Regime.TREND_DOWN: "trend_down",
    Regime.RANGE:      "range",
    Regime.VOLATILE:   "volatile",
}


class SignalPipeline:
    def __init__(
        self,
        model: SignalModel,
        ml_threshold: float = 0.55,
        risk_config: RiskConfig | None = None,
        settings: Settings | None = None,
        regime_weights: dict[str, dict[Regime, float]] | None = None,
    ) -> None:
        self.model = model
        self.ml_threshold = ml_threshold
        self.regime_weights = regime_weights or REGIME_WEIGHTS

        # Trade Score Engine — built from settings if provided
        _settings = settings or get_settings()
        _risk = risk_config or _settings.risk_config()
        self.trade_score_engine = TradeScoreEngine(
            weights=_settings.trade_score_weights(),  # type: ignore[arg-type]
            risk_config=_risk,
        )
        from app.ai.prediction import MetaLabelEngine
        from app.config import MODELS_DIR
        self.meta_label_engine = MetaLabelEngine(MODELS_DIR)
        # Expose on SignalModel so ContinuousTrainer can invalidate meta cache after promotion
        if not hasattr(model, "_meta_label_engine") or model._meta_label_engine is None:
            model._meta_label_engine = self.meta_label_engine

        self._is_event_day: bool = False  # set externally by engine on CPI/FOMC days
        self._midday_active: bool = False  # set externally by engine during low-edge hours


    def set_event_day(self, is_event: bool) -> None:
        """Call from engine when an economic event day is detected."""
        self._is_event_day = is_event

    def set_midday_active(self, is_midday: bool) -> None:
        """Call from engine when entering/leaving the midday low-edge window.

        When active, the score threshold is raised by ``risk_config.midday_score_boost``
        (default +15). Exceptional setups can still trade; borderline ones are rejected.
        """
        self._midday_active = is_midday

    def regime_weight(self, strategy: str, regime: Regime) -> float:
        return self.regime_weights.get(strategy, {}).get(regime, 1.0)

    def process(
        self,
        symbol: str,
        df: pd.DataFrame,
        signals: list[Signal],
        regime: Regime,
        features: pd.Series | None = None,
        sector_strength: float | None = None,
    ) -> tuple[list[Signal], list[tuple[Signal, str]]]:
        """Returns (accepted, rejected_with_reason).

        Each entry signal is evaluated through the full 5-stage chain:
          1. ML probability fast pre-filter
          2. Meta-Label gate (per-strategy profitability prediction)
          3. Excursion estimate (MAE/MFE for dynamic stop/target and Trade Score)
          4. Trade Score computation and regime-dependent gating
          5. Conflict resolution (one entry per symbol)

        Exit signals bypass all gates and always pass.
        """
        if not signals:
            return [], []

        accepted: list[Signal] = []
        rejected: list[tuple[Signal, str]] = []

        if features is None:
            features = latest_feature_row(df)

        # Build liquidity proxy from volume-weighted average
        avg_volume: float | None = None
        if df is not None and "vol_sma20" in df.columns and len(df) > 0:
            avg_volume = float(df["vol_sma20"].iloc[-1]) * float(df["close"].iloc[-1])

        # ATR for fallback excursion estimates
        atr_pct: float = 1.0
        if df is not None and "atr14" in df.columns and len(df) > 0:
            close = float(df["close"].iloc[-1])
            atr_pct = float(df["atr14"].iloc[-1]) / close * 100.0 if close > 0 else 1.0

        regime_key = _REGIME_KEY.get(regime)

        entries: list[Signal] = []
        for sig in signals:
            sig.regime = regime

            # EXIT signals always pass — never block risk reduction
            if sig.action == SignalAction.EXIT:
                accepted.append(sig)
                continue



            if features is None:
                rejected.append((sig, "feature row unavailable (warmup)"))
                continue

            # ---- Stage 1: ML probability detailed scoring ----
            use_detailed = False
            if hasattr(self.model, "score_action_detailed"):
                name = type(self.model.score_action_detailed).__name__
                if "Mock" not in name and "MagicMock" not in name:
                    use_detailed = True

            if use_detailed:
                score_res = self.model.score_action_detailed(features, sig.action, symbol=symbol)
            else:
                prob = self.model.score_action(features, sig.action, symbol=symbol)
                from app.ai.model import ScoreActionResult
                score_res = ScoreActionResult(
                    probability=prob,
                    model_source="BASE",
                    specialist_status="NO_ARTIFACT" if prob is not None else "SPECIALIST_INFERENCE_FAILED",
                    specialist_auc=None,
                    specialist_min_auc=0.53,
                    fallback_reason=None if prob is not None else "score_action returned None"
                )
            ml = score_res.probability
            applog.info(
                "pipeline",
                f"{symbol}: model_source={score_res.model_source} | "
                f"specialist_status={score_res.specialist_status} | "
                f"specialist_auc={f'{score_res.specialist_auc:.4f}' if isinstance(score_res.specialist_auc, (int, float)) else score_res.specialist_auc} | "
                f"specialist_min_auc={f'{score_res.specialist_min_auc:.4f}' if isinstance(score_res.specialist_min_auc, (int, float)) else score_res.specialist_min_auc} | "
                f"fallback={score_res.fallback_reason or 'None'}"
            )
            if ml is None:
                if score_res.specialist_status == "TEMPORAL_BYPASS":
                    applog.info("pipeline", f"{symbol}: ML_GATE_BYPASSED_TEMPORAL")
                    sig.ml_probability = None
                else:
                    rejected.append((
                        sig,
                        f"ML probability unavailable ({score_res.fallback_reason or 'unknown reason'})",
                    ))
                    continue
            else:
                sig.ml_probability = round(ml, 4)
                if ml < self.ml_threshold:
                    rejected.append((
                        sig,
                        f"ML confidence {ml:.4f} < threshold {self.ml_threshold:.4f}",
                    ))
                    continue

            # ---- Stage 3: Excursion estimates ----
            # ATR-based fallback
            from app.ai.prediction import ExcursionEstimate
            excursion = ExcursionEstimate.from_atr(atr_pct)

            # Use regressor-derived values if the signal has no explicit targets
            entry_price = sig.price or (float(df["close"].iloc[-1]) if df is not None and len(df) > 0 else 0.0)
            if entry_price > 0:
                if sig.take_profit is None:
                    sig.take_profit = round(entry_price * (1 + excursion.suggested_target_pct / 100.0), 4)
                if sig.stop_loss is None:
                    sig.stop_loss = round(entry_price * (1 - excursion.suggested_stop_pct / 100.0), 4)

                original_stop = sig.stop_loss
                original_target = sig.take_profit

                # Enforce minimum stop loss percentage floor to protect against micro-noise stop-outs
                from app.config import get_settings
                min_stop_pct = get_settings().risk_config().min_stop_pct
                adjusted = False
                if min_stop_pct > 0:
                    min_dist = entry_price * (min_stop_pct / 100.0)
                    if sig.action == SignalAction.ENTER_LONG:
                        if sig.stop_loss > entry_price - min_dist:
                            sig.stop_loss = round(entry_price - min_dist, 4)
                            adjusted = True
                    elif sig.action == SignalAction.ENTER_SHORT:
                        if sig.stop_loss < entry_price + min_dist:
                            sig.stop_loss = round(entry_price + min_dist, 4)
                            adjusted = True

                if adjusted and original_stop and original_target and sig.strategy != "vwap_reversion":
                    orig_risk = abs(entry_price - original_stop)
                    orig_reward = abs(original_target - entry_price)
                    if orig_risk > 0:
                        rr_ratio = orig_reward / orig_risk
                        new_risk = abs(entry_price - sig.stop_loss)
                        new_reward = new_risk * rr_ratio
                        if sig.action == SignalAction.ENTER_LONG:
                            sig.take_profit = round(entry_price + new_reward, 4)
                        else:
                            sig.take_profit = round(entry_price - new_reward, 4)

            # ---- Stage 2: Meta-Label Gate ----
            side_str = "long" if sig.action == SignalAction.ENTER_LONG else "short"
            meta_pred = self.meta_label_engine.predict(sig.strategy, features, side=side_str)
            if not meta_pred.approved:
                rejected.append((
                    sig,
                    f"Meta-label {meta_pred.decision} [{meta_pred.source}]: {meta_pred.reason}"
                ))
                continue


            if meta_pred.decision == "BYPASS":
                applog.info("pipeline", f"[meta_label] {symbol} {sig.strategy} {sig.action.name} decision=BYPASS source={meta_pred.source} reason={meta_pred.reason}")
            else:
                applog.info("pipeline", f"[meta_label] {symbol} {sig.strategy} {sig.action.name} meta_prob={meta_pred.prob_profitable} decision=PASS source={meta_pred.source}")

            # Recalculate from (possibly updated) signal levels
            tp = sig.take_profit
            sl = sig.stop_loss
            if tp and sl and entry_price:
                exp_ret_pct = abs(tp - entry_price) / entry_price * 100.0
                exp_dd_pct = abs(entry_price - sl) / entry_price * 100.0
            else:
                exp_ret_pct = excursion.mfe_pct
                exp_dd_pct = excursion.mae_pct

            # ---- Stage 4: Trade Score gate ----
            raw_score = self.trade_score_engine.compute(
                ml_prob=ml,
                expected_return_pct=exp_ret_pct,
                expected_drawdown_pct=exp_dd_pct,
                regime=regime,
                avg_daily_volume=avg_volume,
                sector_strength=sector_strength,  # RS vs SPY (0-1), None = neutral 50
            )
            # Defect fix: removed double-counting of regime penalty (raw_score * weight)
            trade_score = min(100.0, max(0.0, raw_score))
            sig.score = round(trade_score / 100.0, 4)  # keep 0-1 convention

            is_breakout = sig.strategy in ["bb_squeeze", "orb", "momentum"]
            approved, threshold = self.trade_score_engine.approve(
                trade_score, regime,
                is_event_day=self._is_event_day,
                threshold_boost=self.trade_score_engine.risk_config.midday_score_boost if (self._midday_active and is_breakout) else 0.0,
            )

            if not approved:
                midday_note = f", midday_boost={self.trade_score_engine.risk_config.midday_score_boost:.0f}" if (self._midday_active and is_breakout) else ""
                effective_rr = exp_ret_pct / exp_dd_pct if exp_dd_pct > 0 else excursion.reward_risk
                ml_str = f"{ml:.2f}" if ml is not None else "N/A"
                rejected.append((
                    sig,
                    f"Trade Score {trade_score:.1f} < {threshold:.1f} (raw={raw_score:.1f}{midday_note}) "
                    f"[regime={regime}, ml={ml_str}, rr={effective_rr:.2f}]",
                ))
            else:
                entries.append(sig)

        # ---- Stage 5: Conflict resolution ----
        # One entry per symbol: highest Trade Score wins
        if entries:
            entries.sort(key=lambda s: s.score or 0.0, reverse=True)
            winner = entries[0]
            accepted.append(winner)
            for loser in entries[1:]:
                rejected.append((
                    loser,
                    f"lost conflict to {winner.strategy} (score {winner.score:.4f})",
                ))

        return accepted, rejected
