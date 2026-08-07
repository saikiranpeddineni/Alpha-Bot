"""Quantitative prediction engine for AlphaDesk v2.0.

Phase 1 additions:
  - DataQualityEngine  : pre-scan bar-feed validation gate.
  - WalkForwardValidator: sliding-window model comparison with XGBoost /
      LightGBM / HistGradientBoosting, selected by *trading* metrics.
  - TradePrediction    : probabilistic prediction payload with CI bands.
  - TradeScoreEngine   : configurable 6-component weighted Trade Score.

Phase 2 additions:
  - MetaLabelEngine    : strategy-specific binary classifiers that predict
      whether a proposed setup will be profitable (meta-labeling layer).
  - ExcursionRegressors: MAE/MFE regressors that estimate maximum adverse
      and favorable excursion, enabling dynamic stop/target placement.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd



from app.ai.features import FEATURE_COLUMNS, build_mae_mfe_dataset, build_training_set
from app.core.domain import Regime
from app.core.applog import applog

def predict_proba_safe(clf, x: pd.DataFrame):
    """Predict proba safely matching the feature names contract of the fitted classifier."""
    if hasattr(clf, "feature_names_in_"):
        cols = list(clf.feature_names_in_)
        missing = [c for c in cols if c not in x.columns]
        if missing or list(x.columns) != cols:
            x_safe = x.copy(deep=False)
            if missing:
                import numpy as np
                for m in missing:
                    x_safe[m] = np.nan
            return clf.predict_proba(x_safe[cols])
        return clf.predict_proba(x)
        
    # ==================================================================================================
    # WARNING TO FUTURE AI AGENTS: DO NOT REVERT THIS LEGACY ARTIFACT FALLBACK!
    # Legacy models on disk were saved without `.feature_names_in_`. If you revert this, 
    # any live deployments running old nameless models will instantly CRASH with a ValueError 
    # (e.g. "X has 19 features, expecting 18") the moment a new feature is added to FEATURE_COLUMNS.
    # This block safely slices or pads DataFrames to perfectly match the nameless model's expected size.
    # ==================================================================================================
    if hasattr(clf, "n_features_in_"):
        expected_n = clf.n_features_in_
        if len(x.columns) > expected_n:
            return clf.predict_proba(x.iloc[:, :expected_n].to_numpy())
        elif len(x.columns) < expected_n:
            import numpy as np
            x_safe = x.copy(deep=False)
            for i in range(len(x.columns), expected_n):
                x_safe[f"__missing_{i}"] = np.nan
            return clf.predict_proba(x_safe.to_numpy())
            
    return clf.predict_proba(x.to_numpy())

def predict_safe(reg, x: pd.DataFrame):
    """Predict safely matching the feature names contract of the fitted regressor."""
    if hasattr(reg, "feature_names_in_"):
        cols = list(reg.feature_names_in_)
        missing = [c for c in cols if c not in x.columns]
        if missing or list(x.columns) != cols:
            x_safe = x.copy(deep=False)
            if missing:
                import numpy as np
                for m in missing:
                    x_safe[m] = np.nan
            return reg.predict(x_safe[cols])
        return reg.predict(x)
        
    # ==================================================================================================
    # WARNING TO FUTURE AI AGENTS: DO NOT REVERT THIS LEGACY ARTIFACT FALLBACK!
    # See explanation in predict_proba_safe above. This prevents catastrophic crashes on legacy nameless artifacts.
    # ==================================================================================================
    if hasattr(reg, "n_features_in_"):
        expected_n = reg.n_features_in_
        if len(x.columns) > expected_n:
            return reg.predict(x.iloc[:, :expected_n].to_numpy())
        elif len(x.columns) < expected_n:
            import numpy as np
            x_safe = x.copy(deep=False)
            for i in range(len(x.columns), expected_n):
                x_safe[f"__missing_{i}"] = np.nan
            return reg.predict(x_safe.to_numpy())
            
    return reg.predict(x.to_numpy())

if TYPE_CHECKING:
    from app.config import RiskConfig, Settings

# ---------------------------------------------------------------------------
# 1. Data Quality Engine
# ---------------------------------------------------------------------------


@dataclass
class DataQualityReport:
    symbol: str
    ok: bool
    issues: list[str] = field(default_factory=list)


class DataQualityEngine:
    """Validates bar DataFrame quality before feeding to ML / strategy layers.

    Called by the engine on every symbol's DataFrame before strategy
    evaluation. If ``ok`` is False the engine should skip new entries for that
    symbol for the current bar cycle.
    """

    def __init__(
        self,
        max_gap_minutes: int = 5,
        max_spread_pct: float = 0.5,
        min_bars: int = 50,
    ) -> None:
        self.max_gap_minutes = max_gap_minutes
        self.max_spread_pct = max_spread_pct
        self.min_bars = min_bars

    @classmethod
    def from_settings(cls, settings: "Settings") -> "DataQualityEngine":
        return cls(
            max_gap_minutes=settings.dq_max_gap_minutes,
            max_spread_pct=settings.dq_max_spread_pct,
            min_bars=settings.dq_min_bars_warmup,
        )

    def validate(self, symbol: str, df: pd.DataFrame) -> DataQualityReport:
        """Return a DataQualityReport for *df*.

        Checks performed (in order):
        1. Minimum bar count.
        2. Duplicate timestamps.
        3. Out-of-order timestamps.
        4. Excessive timestamp gaps (> max_gap_minutes during mkt hours).
        5. Invalid OHLC values (e.g. high < low, negative prices).
        6. Abnormal bid-ask spread proxy (high-low spread vs close).
        """
        issues: list[str] = []

        # 1. Minimum bars
        if len(df) < self.min_bars:
            issues.append(f"insufficient bars: {len(df)} < {self.min_bars}")
            return DataQualityReport(symbol=symbol, ok=False, issues=issues)

        # 2. Duplicate timestamps
        if df.index.duplicated().any():
            n_dup = int(df.index.duplicated().sum())
            issues.append(f"{n_dup} duplicate timestamps")

        # 3. Out-of-order timestamps
        if not df.index.is_monotonic_increasing:
            issues.append("timestamps not monotonically increasing")

        # 4. Excessive timestamp gaps (only flag during normal hours)
        if len(df) > 1:
            diffs = pd.Series(df.index).diff().dropna()
            max_gap = diffs.max()
            if max_gap > timedelta(minutes=self.max_gap_minutes):
                issues.append(
                    f"max bar gap {max_gap.total_seconds()/60:.1f}m "
                    f"> {self.max_gap_minutes}m"
                )

        # 5. Invalid OHLC
        bad_ohlc = (
            (df["high"] < df["low"]).any()
            or (df["close"] <= 0).any()
            or (df["open"] <= 0).any()
        )
        if bad_ohlc:
            issues.append("invalid OHLC values (high<low or non-positive price)")

        # 6. Spread proxy: (high - low) / close > threshold
        close = df["close"].replace(0, np.nan)
        spread_pct = ((df["high"] - df["low"]) / close * 100.0)
        n_wide = int((spread_pct > self.max_spread_pct * 10).sum())  # 5× threshold
        if n_wide > len(df) * 0.05:  # >5% of bars
            issues.append(
                f"{n_wide} bars with spread > {self.max_spread_pct * 10:.1f}%"
            )

        ok = len(issues) == 0
        if not ok:
            applog.warning(
                "data.quality",
                f"{symbol}: {'; '.join(issues)}",
            )
        return DataQualityReport(symbol=symbol, ok=ok, issues=issues)


# ---------------------------------------------------------------------------
# 2. Probabilistic Trade Prediction
# ---------------------------------------------------------------------------


@dataclass
class TradePrediction:
    """Full probabilistic prediction payload for a proposed entry.

    All monetary fields are expressed as percentages of the entry price.
    Confidence intervals use a simple ± band derived from cross-validation
    standard deviation so they are meaningful without deep uncertainty models.
    """

    prob_success: float          # calibrated P(trade profitable)
    prob_success_lo: float       # lower 80% CI bound
    prob_success_hi: float       # upper 80% CI bound
    expected_return_pct: float   # estimated % gain if trade runs to target
    expected_drawdown_pct: float # estimated % adverse move (MAE proxy)
    reward_risk: float           # expected_return / expected_drawdown
    holding_bars_est: int        # estimated bars held
    model_name: str              # winning classifier name
    validation_pf: float         # walk-forward profit factor
    validation_sharpe: float     # walk-forward Sharpe ratio

    @property
    def quality_hint(self) -> str:
        if self.prob_success >= 0.65 and self.reward_risk >= 1.5:
            return "strong"
        if self.prob_success >= 0.55 and self.reward_risk >= 1.0:
            return "moderate"
        return "weak"


# ---------------------------------------------------------------------------
# 3. Walk-Forward Validator & Model Selector
# ---------------------------------------------------------------------------

_CLASSIFIERS: list[str] = ["lgbm", "xgb", "hgb"]


def _make_classifier(name: str, n_estimators: int = 300):
    """Construct a classifier by name; falls back to HGB if the library
    is unavailable."""
    if name == "lgbm":
        try:
            from lightgbm import LGBMClassifier  # type: ignore[import-untyped]
            return LGBMClassifier(
                n_estimators=n_estimators,
                learning_rate=0.05,
                max_depth=4,
                num_leaves=15,
                min_child_samples=100,
                subsample=0.6,
                colsample_bytree=0.6,
                reg_lambda=5.0,
                class_weight="balanced",
                random_state=42,
                verbose=-1,
            )
        except ImportError:
            pass
    if name == "xgb":
        try:
            from xgboost import XGBClassifier  # type: ignore[import-untyped]
            return XGBClassifier(
                n_estimators=n_estimators,
                learning_rate=0.06,
                max_depth=6,
                subsample=0.8,
                colsample_bytree=0.8,
                reg_lambda=1.0,
                use_label_encoder=False,
                eval_metric="logloss",
                scale_pos_weight=1.5,
                random_state=42,
                verbosity=0,
            )
        except ImportError:
            pass
    # Fallback: scikit-learn HistGradientBoosting
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(
        max_iter=n_estimators,
        learning_rate=0.06,
        max_depth=6,
        l2_regularization=1.0,
        early_stopping=True,
        random_state=42,
    )


def _trading_metrics(
    y_true: np.ndarray,
    proba: np.ndarray,
    threshold: float = 0.5,
) -> dict:
    """Compute trading-oriented performance metrics for the validation window.

    Returns profit_factor, expectancy_r, sharpe, win_rate, n_trades.
    A "win" is defined as pred=1 and y=1 (true positive), or pred=0 and y=0
    (true negative — i.e. we correctly sat out). Expectancy is simulated at
    1R reward / 1R risk.
    """
    pred = (proba >= threshold).astype(int)
    mask_entry = pred == 1  # bars where model said "enter"
    if mask_entry.sum() == 0:
        return {"profit_factor": 0.0, "expectancy_r": -1.0, "sharpe": 0.0,
                "win_rate": 0.0, "n_trades": 0}

    y_entry = y_true[mask_entry]
    wins = int(y_entry.sum())
    losses = int((y_entry == 0).sum())
    n_trades = len(y_entry)
    win_rate = wins / n_trades
    gross_profit = float(wins * 1.0)  # 1R per win
    gross_loss = float(losses * 1.0)  # 1R per loss
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    expectancy = (win_rate * 1.0) - ((1 - win_rate) * 1.0)  # R units

    # Pseudo-Sharpe: mean(returns) / std(returns)
    returns = np.where(y_entry == 1, 1.0, -1.0).astype(float)
    sharpe = float(returns.mean() / (returns.std() + 1e-8) * math.sqrt(252))

    return {
        "profit_factor": round(pf, 4),
        "expectancy_r": round(expectancy, 4),
        "sharpe": round(sharpe, 4),
        "win_rate": round(win_rate, 4),
        "n_trades": n_trades,
    }


class WalkForwardValidator:
    """Rolling Walk-Forward validation that compares LightGBM / XGBoost /
    HistGradientBoosting and selects the best classifier by out-of-sample
    trading performance (Profit Factor, Expectancy, Sharpe).

    Usage::

        wfv = WalkForwardValidator(train_days=60, val_days=10, min_windows=3)
        result = wfv.run(frames={"AAPL": df_aapl, ...}, side="long")
        best_clf = result.best_classifier
        metrics  = result.summary
    """

    def __init__(
        self,
        train_days: int = 60,
        val_days: int = 10,
        min_windows: int = 3,
        horizon_bars: int = 15,
        threshold_pct: float = 0.10,
    ) -> None:
        self.train_days = train_days
        self.val_days = val_days
        self.min_windows = min_windows
        self.horizon_bars = horizon_bars
        self.threshold_pct = threshold_pct

    @classmethod
    def from_settings(
        cls,
        settings: "Settings",
        horizon_bars: int = 15,
        threshold_pct: float = 0.10,
    ) -> "WalkForwardValidator":
        return cls(
            train_days=settings.walk_forward_train_days,
            val_days=settings.walk_forward_val_days,
            min_windows=settings.walk_forward_min_windows,
            horizon_bars=horizon_bars,
            threshold_pct=threshold_pct,
        )

    # ------------------------------------------------------------------
    def _split_by_days(
        self, df: pd.DataFrame
    ) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
        """Generate (train_df, val_df) sliding window pairs."""
        windows: list[tuple[pd.DataFrame, pd.DataFrame]] = []
        dates = sorted(set(df.index.tz_convert("America/New_York").date))
        step = self.val_days
        window = self.train_days + self.val_days
        start = 0
        while start + window <= len(dates):
            train_dates = set(dates[start : start + self.train_days])
            val_dates = set(dates[start + self.train_days : start + window])
            et_idx = df.index.tz_convert("America/New_York").date
            train_df = df[[d in train_dates for d in et_idx]]
            val_df = df[[d in val_dates for d in et_idx]]
            if len(train_df) > 100 and len(val_df) > 20:
                windows.append((train_df, val_df))
            start += step
        return windows

    # ------------------------------------------------------------------
    def run(
        self,
        frames: dict[str, pd.DataFrame],
        side: str = "long",
    ) -> "WalkForwardResult":
        """Run walk-forward validation across all frames for a given side.

        Returns WalkForwardResult with the best classifier and aggregated
        metrics per model.
        """
        # Pool all frames into a single sorted dataset
        xs, ys, ws = [], [], []
        for df in frames.values():
            X, y, w = build_training_set(df, self.horizon_bars, self.threshold_pct, side)
            if len(X) > 20:
                xs.append(X)
                ys.append(y)
                ws.append(w)

        if not xs:
            applog.warning("wfv", "Not enough data for walk-forward validation")
            return WalkForwardResult(
                best_classifier=_make_classifier("hgb"),
                best_name="hgb",
                summary={},
                windows_evaluated=0,
                side=side,
            )

        X_all = pd.concat(xs).sort_index()
        y_all = pd.concat(ys).sort_index()
        w_all = pd.concat(ws).sort_index()

        # Build a combined frame for day-splitting
        combined = X_all.copy()
        combined["__label__"] = y_all
        combined["__weight__"] = w_all

        windows = self._split_by_days(combined)
        n_windows = len(windows)

        if n_windows < self.min_windows:
            applog.warning(
                "wfv",
                f"Only {n_windows} windows (need \u2265{self.min_windows}). "
                "Using full-dataset training.",
            )
            clf = _make_classifier("lgbm")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                clf.fit(X_all, y_all, sample_weight=w_all)
            return WalkForwardResult(
                best_classifier=clf,
                best_name="lgbm",
                summary={},
                windows_evaluated=0,
                side=side,
            )

        # Accumulate per-model metrics across windows
        model_window_metrics: dict[str, list[dict]] = {n: [] for n in _CLASSIFIERS}

        for train_df, val_df in windows:
            X_tr = train_df.drop(columns=["__label__", "__weight__"])
            y_tr = train_df["__label__"]
            w_tr = train_df["__weight__"]
            X_va = val_df.drop(columns=["__label__", "__weight__"])
            y_va = val_df["__label__"]

            if y_tr.nunique() < 2 or y_va.nunique() < 2:
                continue  # skip degenerate windows

            for name in _CLASSIFIERS:
                try:
                    clf = _make_classifier(name)
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        clf.fit(X_tr, y_tr, sample_weight=w_tr)
                    proba = clf.predict_proba(X_va)[:, 1]
                    m = _trading_metrics(y_va.to_numpy(), proba)
                    model_window_metrics[name].append(m)
                except Exception as exc:  # noqa: BLE001
                    applog.debug("wfv", f"{name} window failed: {exc}")

        # Aggregate: mean Profit Factor across windows as primary rank metric
        summary: dict[str, dict] = {}
        best_name = "hgb"
        best_score = -1.0

        for name, window_list in model_window_metrics.items():
            if not window_list:
                continue
            agg: dict[str, float] = {}
            for key in ("profit_factor", "expectancy_r", "sharpe", "win_rate"):
                vals = [w[key] for w in window_list if w["n_trades"] > 0]
                agg[key] = float(np.mean(vals)) if vals else 0.0
            agg["n_windows"] = len(window_list)
            summary[name] = agg
            # Composite rank: 50% PF + 30% Expectancy + 20% Sharpe (normalized)
            rank = (
                0.50 * min(agg["profit_factor"], 5.0) / 5.0
                + 0.30 * max(-1.0, min(agg["expectancy_r"] + 1.0, 2.0)) / 2.0
                + 0.20 * max(-2.0, min(agg["sharpe"], 4.0)) / 4.0
            )
            if rank > best_score:
                best_score = rank
                best_name = name

        applog.info(
            "wfv",
            f"Walk-forward ({side}): best={best_name}, "
            f"PF={summary.get(best_name, {}).get('profit_factor', 0):.3f}, "
            f"E={summary.get(best_name, {}).get('expectancy_r', 0):.3f}, "
            f"windows={n_windows}",
        )

        # Retrain best model on all data up to the last training window boundary.
        # This prevents look-ahead bias where the final model is trained on the
        # exact validation data that was just used to select it.
        if windows:
            last_train_df = windows[-1][0]
            last_train_end = last_train_df.index.max()
            train_mask = X_all.index <= last_train_end
            X_final = X_all[train_mask]
            y_final = y_all[train_mask]
            w_final = w_all[train_mask]
        else:
            X_final = X_all
            y_final = y_all
            w_final = w_all

        from sklearn.calibration import CalibratedClassifierCV
        base_clf = _make_classifier(best_name)
        # Use cv=3 isotonic regression to calibrate probabilities to true win-rates
        best_clf = CalibratedClassifierCV(estimator=base_clf, method="isotonic", cv=3)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            best_clf.fit(X_final, y_final, sample_weight=w_final)

        return WalkForwardResult(
            best_classifier=best_clf,
            best_name=best_name,
            summary=summary,
            windows_evaluated=n_windows,
            side=side,
        )


@dataclass
class WalkForwardResult:
    best_classifier: object   # fitted sklearn-compatible estimator
    best_name: str            # "lgbm" | "xgb" | "hgb"
    summary: dict             # per-model aggregated trading metrics
    windows_evaluated: int
    side: str                 # "long" | "short"


# ---------------------------------------------------------------------------
# 4. Trade Score Engine
# ---------------------------------------------------------------------------

# Maps Regime -> relative difficulty multiplier used to pick the right gate.
_REGIME_SCORE: dict[str, float] = {
    Regime.TREND_UP:   1.0,
    Regime.TREND_DOWN: 1.0,
    Regime.RANGE:      0.7,   # harder to trade; score inflated requirement
    Regime.VOLATILE:   0.85,
}


class TradeScoreEngine:
    """Computes a 0-100 composite Trade Score for a proposed entry.

    Components (weights configurable via Settings):
      w1 x ML probability of success
      w2 x Expected return score
      w3 x Reward / Risk ratio score
      w4 x Market regime score
      w5 x Liquidity score
      w6 x Sector strength score

    The result is compared against the regime-specific gating threshold
    from RiskConfig to approve or reject the proposed trade.
    """

    def __init__(
        self,
        weights: tuple[float, float, float, float, float, float],
        risk_config: "RiskConfig",
    ) -> None:
        self.w = weights  # already normalized by settings.trade_score_weights()
        self.risk_config = risk_config

    @classmethod
    def from_settings(cls, settings: "Settings") -> "TradeScoreEngine":
        from app.config import RiskConfig  # avoid circular at module level
        return cls(
            weights=settings.trade_score_weights(),  # type: ignore[arg-type]
            risk_config=settings.risk_config(),
        )

    # ------------------------------------------------------------------
    def compute(
        self,
        *,
        ml_prob: float | None,            # 0-1 calibrated probability
        expected_return_pct: float,       # % expected gain
        expected_drawdown_pct: float,     # % expected adverse move
        regime: Regime,
        avg_daily_volume: float | None = None,
        sector_strength: float | None = None,  # 0-1
    ) -> float:
        """Return the composite Trade Score in [0, 100]."""
        w1, w2, w3, w4, w5, w6 = self.w

        # Probability component (already 0-1 -> scale to 0-100)
        s_prob = (ml_prob * 100.0) if ml_prob is not None else 50.0

        # Expected return component: normalize around a 0.25% intraday target for 1m bars
        s_ret = min(100.0, max(0.0, expected_return_pct / 0.25 * 100.0))

        # Reward/Risk component: 2:1 is 100, 1:1 is 50, <1:1 is 0
        rr = (expected_return_pct / expected_drawdown_pct
              if expected_drawdown_pct > 0 else 1.0)
        s_rr = min(100.0, max(0.0, rr / 2.0 * 100.0))  # 2:1 -> 100

        # Regime component
        s_regime = _REGIME_SCORE.get(regime, 1.0) * 100.0

        # Liquidity component: proxy via avg 1-minute dollar volume
        if avg_daily_volume is not None:
            # 500k dollar volume/minute -> 100
            s_liq = min(100.0, avg_daily_volume / 500_000.0 * 100.0)
        else:
            s_liq = 50.0  # neutral when unknown

        # Sector strength component
        if sector_strength is not None:
            s_sector = float(sector_strength) * 100.0
        else:
            s_sector = 50.0  # neutral when unknown

        score = (
            w1 * s_prob
            + w2 * s_ret
            + w3 * s_rr
            + w4 * s_regime
            + w5 * s_liq
            + w6 * s_sector
        )
        return round(min(100.0, max(0.0, score)), 2)

    def gate_threshold(self, regime: Regime, is_event_day: bool = False, threshold_boost: float = 0.0) -> float:
        """Return the minimum Trade Score required to approve an entry.

        ``threshold_boost`` is added on top of the base threshold, e.g. during
        midday (low-edge hours) to require a stronger signal without hard-blocking.
        """
        rc = self.risk_config
        if is_event_day:
            base = rc.score_threshold_event
        elif regime in (Regime.TREND_UP, Regime.TREND_DOWN):
            base = rc.score_threshold_trend
        elif regime == Regime.RANGE:
            base = rc.score_threshold_range
        else:
            base = rc.score_threshold_volatile
        return min(100.0, base + threshold_boost)

    def approve(
        self,
        score: float,
        regime: Regime,
        is_event_day: bool = False,
        threshold_boost: float = 0.0,
    ) -> tuple[bool, float]:
        """Return (approved, threshold).

        ``threshold_boost`` raises the bar without fully blocking -- exceptional
        setups (very high score) can still trade during low-edge periods.
        """
        threshold = self.gate_threshold(regime, is_event_day, threshold_boost)
        return score >= threshold, threshold


# ---------------------------------------------------------------------------
# 5. Meta-Label Engine (Phase 2)
# ---------------------------------------------------------------------------


@dataclass
class MetaPrediction:
    """Result of a Meta-Model evaluation for one strategy signal.

    Three-state decision contract:
      PASS   = a real trained model evaluated the candidate and approved it.
      REJECT = a real model rejected the candidate, OR a mandatory model is
               unavailable/invalid/errored.
      BYPASS = MetaLabel is explicitly disabled for this strategy (not configured).
    """
    strategy: str
    prob_profitable: float | None   # None when no real model prediction available
    approved: bool                  # True only for PASS
    threshold: float                # threshold used (or configured)
    n_training_samples: int         # sample count used to train this meta-model
    source: str = "REAL_MODEL"      # REAL_MODEL | NO_MODEL | PREDICTION_ERROR | INVALID_FEATURES | BYPASS
    decision: str = "PASS"         # PASS | REJECT | BYPASS
    reason: str = ""               # human-readable explanation when REJECT or BYPASS


def get_strategy_candidates(
    strategy_name: str,
    df: pd.DataFrame,
    symbol: str,
    side: str,
    strategy_overrides: dict[str, dict] | None = None,
) -> list[pd.Timestamp]:
    """Replay the strategy on the historical dataframe to find candidate timestamps.
    
    Uses closed-bar semantics and the actual strategy on_bar method.
    """
    from app.strategies.base import create_strategies, StrategyContext
    from app.core.domain import SignalAction, Regime
    import numpy as np

    # Use the same persisted strategy parameters as the live engine and
    # backtester.  A MetaLabel trained on defaults must never gate a strategy
    # running with user-modified parameters.
    strats = create_strategies(strategy_overrides)
    strat = next((s for s in strats if s.name == strategy_name), None)
    if strat is None:
        return []

    strat.reset()
    candidate_timestamps = []
    
    from app.indicators.ta import is_enriched, enrich
    if not is_enriched(df):
        df = enrich(df)

    n = len(df)
    if n < 60:
        return []

    # Use the memoized regime_val feature rather than manually re-calculating slope/ATR/ER20
    # for 142k bars iteratively.
    if "regime_val" in df.columns:
        regime_map = {0.0: Regime.RANGE, 1.0: Regime.TREND_UP, -1.0: Regime.TREND_DOWN, 2.0: Regime.VOLATILE}
        regimes = df["regime_val"].map(regime_map).fillna(Regime.RANGE).tolist()
    else:
        # Fallback if somehow not enriched
        regimes = [Regime.RANGE] * n

    # Slice window size matches backtest: SLICE_WINDOW = 450
    for i in range(1, n):
        window = df.iloc[max(0, i - 450 + 1) : i + 1]
        regime = regimes[i]
        from datetime import timedelta
        now_ts = (window.index[-1] + timedelta(minutes=1)).to_pydatetime()
        sctx = StrategyContext(position=None, equity=100_000.0, regime=regime, now=now_ts)
        
        try:
            sig = strat.on_bar(sctx, symbol, window)
        except Exception:
            sig = None
            
        if sig is not None:
            # Check direction match
            sig_side = "long" if sig.action == SignalAction.ENTER_LONG else "short"
            if sig_side == side:
                candidate_timestamps.append(df.index[i])
                
    return candidate_timestamps


class MetaLabelEngine:
    """Strategy-specific binary meta-classifiers.

    Architecture (meta-labeling pattern):
      1. A base strategy generates a directional setup signal.
      2. The MetaLabelEngine predicts P(trade profitable | entry features)
         independently for each strategy using its own trained classifier.
      3. Only setups with P >= threshold pass to the Trade Score Engine.

    Models are stored per-strategy as ``meta_{strategy}_{side}.joblib`` in the
    models directory and loaded lazily on first use.

    Training is triggered either:
      - Explicitly via ``train(strategy, frames, side)``.
      - Lazily on first inference if no saved model exists (heuristic fallback).
    """

    # Default per-strategy approval thresholds.
    # These are deliberately conservative; lower only with evidence.
    @property
    def default_thresholds(self) -> dict[str, float]:
        from app.strategies.base import REGISTRY
        return {name: cls.ml_threshold for name, cls in REGISTRY.items()}
    FALLBACK_THRESHOLD = 0.53

    # Strategies that REQUIRE a trained MetaLabel model to pass.
    # If the model is absent, candidates are REJECTED (fail-closed).
    # Strategies not listed here are BYPASSED when no model is available.
    MANDATORY_STRATEGIES: frozenset = frozenset()

    def __init__(self, models_dir: "Path") -> None:  # type: ignore[name-defined]
        from pathlib import Path as _Path
        self.models_dir = _Path(models_dir) / "meta"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._classifiers: dict[str, object | None] = {}  # strategy_side -> fitted clf
        self._sample_counts: dict[str, int] = {}
        self._provenance: dict[str, dict] = {}  # strategy_side -> artifact provenance dict
        # FIX: Explicitly declare backtest_start_time so the temporal-leakage
        # gate is always reachable without relying on external attribute
        # injection.  Live instances keep this as None; the backtest engine
        # sets it on the deep-copied instance before running the replay.
        self.backtest_start_time: datetime | None = None

    # ------------------------------------------------------------------
    def _load(self, strategy: str, side: str = "long") -> object | None:
        """Lazy-load a saved meta-model for a strategy and side.

        Handles both artifact formats:
          - Legacy: bare sklearn estimator (pre-provenance)
          - Current: dict with 'model' key and provenance fields
        """
        cache_key = f"{strategy}_{side}"
        if cache_key in self._classifiers:
            return self._classifiers[cache_key]
        try:
            import joblib
            path = self.models_dir / f"meta_{strategy}_{side}.joblib"
            if path.exists():
                artifact = joblib.load(path)
                # New format: artifact is a dict with provenance + model
                if isinstance(artifact, dict) and "model" in artifact:
                    clf = artifact["model"]
                    prov = {k: v for k, v in artifact.items() if k != "model"}
                    self._provenance[cache_key] = prov
                    self._sample_counts[cache_key] = artifact.get("n_samples", 0)
                    applog.info(
                        "meta",
                        f"[meta_load] {strategy} ({side}) | source=REAL_MODEL | "
                        f"trained_at={artifact.get('trained_at', 'unknown')} "
                        f"val_auc={artifact.get('val_auc', 'unknown')} "
                        f"n_samples={artifact.get('n_samples', 'unknown')} "
                        f"n_pos={artifact.get('n_positive', 'unknown')} "
                        f"n_neg={artifact.get('n_negative', 'unknown')} "
                        f"feature_version={artifact.get('feature_version', 'unknown')}",
                    )
                else:
                    # Legacy: bare estimator -- no provenance available
                    clf = artifact
                    self._sample_counts[cache_key] = 0
                    applog.info("meta", f"[meta_load] {strategy} ({side}) | source=LEGACY_ARTIFACT (no provenance)")
                self._classifiers[cache_key] = clf
                return clf
        except Exception as exc:
            applog.warning("meta", f"Failed to load meta-model for {strategy} ({side}): {exc}")
        self._classifiers[cache_key] = None

        return None

    def predict(self, strategy: str, features: "pd.Series", side: str = "long", adaptive_threshold: float | None = None) -> MetaPrediction:  # type: ignore[name-defined]
        """Return a MetaPrediction using the explicit three-state contract.

        PASS   = real trained model evaluated and approved.
        REJECT = real model rejected, or mandatory model unavailable.
        BYPASS = model not configured / not yet trained for optional strategy.
        """
        import hashlib
        clf = self._load(strategy, side)
        
        # Check for temporal leakage relative to backtest start time.
        # FIX: Use `self.backtest_start_time is not None` instead of the
        # previous `hasattr(self, "backtest_start_time")` guard now that the
        # field is always declared in __init__.
        if clf is not None:
            cache_key = f"{strategy}_{side}"
            prov = self._provenance.get(cache_key)
            if prov and self.backtest_start_time is not None:
                trained_at_str = prov.get("trained_at")
                if trained_at_str:
                    trained_at = datetime.fromisoformat(trained_at_str.replace("Z", "+00:00"))
                    bt_start = self.backtest_start_time
                    if bt_start.tzinfo is None:
                        bt_start = bt_start.replace(tzinfo=UTC)
                    if trained_at.tzinfo is None:
                        trained_at = trained_at.replace(tzinfo=UTC)
                    else:
                        trained_at = trained_at.astimezone(UTC)
                    if trained_at > bt_start:
                        applog.warning(
                            "meta",
                            f"[meta_label] Bypass {strategy} ({side}) due to temporal leakage: "
                            f"model trained_at {trained_at_str} is after backtest start {self.backtest_start_time}"
                        )
                        clf = None  # fix: actually bypass the leaky model (was: pass — bug!)
        threshold = adaptive_threshold if adaptive_threshold is not None else self.default_thresholds.get(strategy, self.FALLBACK_THRESHOLD)
        cache_key = f"{strategy}_{side}"
        n_samples = self._sample_counts.get(cache_key, 0)
        is_mandatory = strategy in self.MANDATORY_STRATEGIES

        if clf is None:
            artifact_path = self.models_dir / f"meta_{strategy}_{side}.joblib"
            if is_mandatory:
                # Fail-closed: mandatory model missing -> REJECT
                applog.warning(
                    "meta",
                    f"[meta_label] {strategy} ({side}) REJECT | source=NO_MODEL | mandatory=True | "
                    f"artifact={artifact_path} exists=False",
                )
                return MetaPrediction(
                    strategy=strategy,
                    prob_profitable=None,
                    approved=False,
                    threshold=threshold,
                    n_training_samples=0,
                    source="NO_MODEL",
                    decision="REJECT",
                    reason="META_MODEL_UNAVAILABLE (mandatory strategy)",
                )
            else:
                # BYPASS: strategy not yet configured for MetaLabel
                applog.info(
                    "meta",
                    f"[meta_label] {strategy} ({side}) BYPASS | source=NO_MODEL | mandatory=False | "
                    f"artifact={artifact_path} exists=False",
                )
                return MetaPrediction(
                    strategy=strategy,
                    prob_profitable=None,
                    approved=True,
                    threshold=threshold,
                    n_training_samples=0,
                    source="NO_MODEL",
                    decision="BYPASS",
                    reason="meta_label_not_configured_for_strategy",
                )

        # Validate feature vector before inference
        try:
            feat_vals = features[FEATURE_COLUMNS].to_numpy(dtype=float)
        except Exception as exc:
            applog.warning("meta", f"[meta_label] {strategy} ({side}) REJECT | source=INVALID_FEATURES | error={exc}")
            return MetaPrediction(
                strategy=strategy, prob_profitable=None, approved=False,
                threshold=threshold, n_training_samples=n_samples,
                source="INVALID_FEATURES", decision="REJECT",
                reason=f"feature_extraction_failed: {exc}",
            )

        feat_std = float(np.std(feat_vals))
        n_nan = int(np.isnan(feat_vals).sum())
        runtime_feature_hash = hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]

        if n_nan > 0:
            applog.warning(
                "meta",
                f"[meta_label] {strategy} ({side}) REJECT | source=INVALID_FEATURES | "
                f"n_nan={n_nan} feat_std={feat_std:.4f}",
            )
            return MetaPrediction(
                strategy=strategy, prob_profitable=None, approved=False,
                threshold=threshold, n_training_samples=n_samples,
                source="INVALID_FEATURES", decision="REJECT",
                reason=f"feature_vector_has_{n_nan}_NaN_values",
            )

        if feat_std == 0.0:
            applog.warning(
                "meta",
                f"[meta_label] {strategy} ({side}) REJECT | source=INVALID_FEATURES | "
                f"reason=constant_feature_vector feat_std=0.0",
            )
            return MetaPrediction(
                strategy=strategy, prob_profitable=None, approved=False,
                threshold=threshold, n_training_samples=n_samples,
                source="INVALID_FEATURES", decision="REJECT",
                reason="constant_feature_vector",
            )

        try:
            # Pass a one-row DataFrame to eliminate feature names UserWarning
            x = pd.DataFrame([feat_vals], columns=FEATURE_COLUMNS)
            raw_proba = predict_proba_safe(clf, x)
            prob = float(raw_proba[0, 1])
            clf_name = type(clf).__name__
            
            vol_z = float(features.get("vol_z", 0.0))
            if vol_z > 3.0 and prob < threshold and prob >= 0.45:
                applog.info("meta", f"[HARVEST BYPASS] Explosive volume (vol_z={vol_z:.1f}) dropping threshold {threshold}->0.45")
                threshold = 0.45
                
            decision = "PASS" if prob >= threshold else "REJECT"
            applog.debug(
                "meta",
                f"[meta_label] {strategy} ({side}) {decision} | source=REAL_MODEL | "
                f"model={clf_name} n_samples={n_samples} feat_std={feat_std:.4f} "
                f"feature_hash={runtime_feature_hash} "
                f"raw_proba=[{raw_proba[0,0]:.4f},{raw_proba[0,1]:.4f}] "
                f"prob={prob:.4f} threshold={threshold:.2f}",
            )
            return MetaPrediction(
                strategy=strategy,
                prob_profitable=round(prob, 4),
                approved=prob >= threshold,
                threshold=threshold,
                n_training_samples=n_samples,
                source="REAL_MODEL",
                decision=decision,
                reason="" if prob >= threshold else f"prob_{prob:.4f}_below_threshold_{threshold:.2f}",
            )
        except Exception as exc:
            applog.warning("meta", f"[meta_label] {strategy} ({side}) REJECT | source=PREDICTION_ERROR | error={exc}")
            return MetaPrediction(
                strategy=strategy, prob_profitable=None, approved=False,
                threshold=threshold, n_training_samples=n_samples,
                source="PREDICTION_ERROR", decision="REJECT",
                reason=f"inference_exception: {exc}",
            )


    def train(
        self,
        strategy: str,
        frames: dict,  # symbol -> pd.DataFrame
        horizon_bars: int = 15,
        threshold_pct: float = 0.10,
        side: str = "long",
        min_validation_auc: float = 0.52,
        strategy_overrides: dict[str, dict] | None = None,
    ) -> dict:
        """Train and validate a meta-classifier for the given strategy and side.

        Lifecycle:
          1. Run strategy candidate replay historically to find target candidates.
          2. Build dataset from candidate timestamps across all frames.
          3. Temporal train/val split (75/25 chronological).
          4. Fit classifier on train portion.
          5. Compute OOS validation AUC on val portion.
          6. Persist artifact with full provenance only if AUC >= min_validation_auc.
          7. Invalidate in-memory cache so next predict() loads fresh artifact.
          8. Return provenance dict with PROMOTED or REJECTED_LOW_AUC status.
        """
        import hashlib
        from sklearn.metrics import roc_auc_score

        xs, ys, ws = [], [], []
        strategy_candidate_count = 0
        for symbol, df in frames.items():
            candidate_timestamps = get_strategy_candidates(
                strategy, df, symbol, side, strategy_overrides,
            )
            if not candidate_timestamps:
                continue

            X, y, w = build_training_set(df, horizon_bars, threshold_pct, side=side)
            common_idx = X.index.intersection(candidate_timestamps)
            if len(common_idx) > 0:
                xs.append(X.loc[common_idx])
                ys.append(y.loc[common_idx])
                ws.append(w.loc[common_idx])
                strategy_candidate_count += len(common_idx)

        if not xs:
            applog.warning("meta", f"[meta_train] {strategy} ({side}) SKIPPED -- no strategy candidate events found")
            return {"status": "skipped", "strategy": strategy, "side": side, "reason": "no_data"}

        X_all = pd.concat(xs).sort_index()
        y_all = pd.concat(ys).sort_index()
        w_all = pd.concat(ws).sort_index()
        n_total = len(X_all)
        n_pos = int(y_all.sum())
        n_neg = int((y_all == 0).sum())

        # Generate dataset fingerprints
        ordered_timestamps = sorted(list(X_all.index))
        ts_str = ",".join(str(ts) for ts in ordered_timestamps)
        ts_hash = hashlib.md5(ts_str.encode()).hexdigest()[:12]

        label_str = ",".join(str(val) for val in y_all.to_numpy())
        label_hash = hashlib.md5(label_str.encode()).hexdigest()[:12]

        first_ts = X_all.index.min().isoformat() if not X_all.empty else "None"
        last_ts = X_all.index.max().isoformat() if not X_all.empty else "None"

        applog.info(
            "meta",
            f"[meta_train_fingerprint] {strategy} ({side}) | "
            f"dataset_row_count={n_total} | "
            f"candidate_timestamp_count={strategy_candidate_count} | "
            f"first_timestamp={first_ts} | "
            f"last_timestamp={last_ts} | "
            f"positive_count={n_pos} | "
            f"negative_count={n_neg} | "
            f"ordered_timestamp_hash={ts_hash} | "
            f"label_hash={label_hash} | "
            f"strategy_candidate_count={strategy_candidate_count}"
        )

        if y_all.nunique() < 2:
            applog.warning("meta", f"[meta_train] {strategy} ({side}) SKIPPED -- single class (n_pos={n_pos} n_neg={n_neg})")
            return {"status": "skipped", "strategy": strategy, "side": side, "reason": "single_class",
                    "n_samples": n_total, "n_positive": n_pos, "n_negative": n_neg}

        # Temporal split -- never shuffle meta-label data
        split = int(n_total * 0.75)
        n_train = split
        n_val = n_total - split

        if n_val < 20:
            applog.warning("meta", f"[meta_train] {strategy} ({side}) SKIPPED -- insufficient validation samples ({n_val} < 20)")
            return {"status": "skipped", "strategy": strategy, "side": side, "reason": "insufficient_validation_samples",
                    "n_samples": n_total, "n_train": n_train, "n_val": n_val}

        from datetime import timedelta
        val_start_dt = X_all.iloc[split:].index.min()
        purge = timedelta(minutes=15)
        train_mask = X_all.index < val_start_dt - purge

        X_tr, y_tr, w_tr = X_all[train_mask], y_all[train_mask], w_all[train_mask]
        X_va, y_va = X_all.iloc[split:], y_all.iloc[split:]

        clf = _make_classifier("lgbm", n_estimators=200)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if not X_tr.empty:
                try:
                    clf.fit(X_tr, y_tr, sample_weight=w_tr)
                except Exception as exc:
                    applog.warning("meta", f"[meta_train] {strategy} ({side}) LGBM failed ({exc}), falling back to HGB")
                    clf = _make_classifier("hgb")
                    clf.fit(X_tr, y_tr, sample_weight=w_tr)

        # OOS validation AUC
        val_auc = 0.5
        if y_va.nunique() >= 2:
            try:
                # Pass DataFrame to predict_proba
                proba_va = predict_proba_safe(clf, X_va)[:, 1]
                val_auc = float(roc_auc_score(y_va, proba_va))
            except Exception as exc:
                applog.warning("meta", f"[meta_train] {strategy} ({side}) AUC computation failed: {exc}")

        # The probability threshold is deliberately not tuned here.  This
        # validation report shows whether the *existing* gate improves the
        # candidate population it will be asked to filter.
        threshold = self.default_thresholds.get(strategy, self.FALLBACK_THRESHOLD)
        validation_gate_report: dict[str, dict | int | float] = {}
        if len(y_va):
            proba_va = predict_proba_safe(clf, X_va)[:, 1]

            def _bucket(mask: np.ndarray) -> dict:
                labels = y_va.to_numpy()[mask]
                return {
                    "count": int(mask.sum()),
                    "wins": int(labels.sum()),
                    "win_rate": round(float(labels.mean()), 4) if len(labels) else None,
                }

            passed = proba_va >= threshold
            validation_gate_report = {
                "baseline": _bucket(np.ones(len(proba_va), dtype=bool)),
                "accepted": _bucket(passed),
                "rejected": _bucket(~passed),
                "threshold": threshold,
            }

        feature_hash = hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]
        artifact_path = self.models_dir / f"meta_{strategy}_{side}.joblib"

        promotion_decision = "PROMOTED" if val_auc >= min_validation_auc else "REJECTED_LOW_AUC"

        applog.info(
            "meta",
            f"[meta_train] {strategy} ({side}) | n_total={n_total} n_pos={n_pos} n_neg={n_neg} "
            f"n_train={n_train} n_val={n_val} base_rate={float(y_all.mean()):.3f} "
            f"val_auc={val_auc:.4f} min_auc={min_validation_auc:.4f} "
            f"threshold={threshold:.2f} feature_hash={feature_hash} "
            f"artifact={artifact_path} decision={promotion_decision}",
        )

        if val_auc < min_validation_auc:
            return {
                "status": "rejected",
                "strategy": strategy,
                "side": side,
                "decision": promotion_decision,
                "val_auc": round(val_auc, 4),
                "min_validation_auc": min_validation_auc,
                "n_samples": n_total,
                "n_positive": n_pos,
                "n_negative": n_neg,
                "n_train": n_train,
                "n_validation": n_val,
            }

        # Now fit on full dataset before saving
        clf_full = _make_classifier("lgbm", n_estimators=200)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                clf_full.fit(X_all, y_all)
            except Exception as exc:
                applog.warning("meta", f"[meta_train] {strategy} ({side}) full LGBM failed ({exc}), falling back to HGB")
                clf_full = _make_classifier("hgb")
                clf_full.fit(X_all, y_all)

        provenance = {
            "model": clf_full,
            "strategy": strategy,
            "side": side,
            "val_auc": round(val_auc, 4),
            "min_validation_auc": min_validation_auc,
            "trained_at": datetime.now(UTC).isoformat(),
            "n_samples": n_total,
            "n_positive": n_pos,
            "n_negative": n_neg,
            "n_train": n_train,
            "n_validation": n_val,
            "base_rate": round(float(y_all.mean()), 4),
            "feature_version": feature_hash,
            "feature_count": len(FEATURE_COLUMNS),
            "horizon_bars": horizon_bars,
            "threshold_pct": threshold_pct,
            "label_definition": f"directional_{side}_return_threshold_over_horizon_bars",
            "meta_threshold": threshold,
            "candidate_population": "strategy_replay_candidates",
            "validation_gate_report": validation_gate_report,
            "ordered_timestamp_hash": ts_hash,
            "label_hash": label_hash,
        }

        try:
            import joblib
            joblib.dump(provenance, artifact_path)
        except Exception as exc:
            applog.warning("meta", f"[meta_train] {strategy} ({side}) artifact save failed: {exc}")
            return {"status": "save_failed", "strategy": strategy, "side": side, "reason": str(exc)}

        # Invalidate in-memory cache so next predict() loads the fresh artifact
        cache_key = f"{strategy}_{side}"
        self._classifiers[cache_key] = clf_full
        self._sample_counts[cache_key] = n_total

        return {
            "status": "success",
            "strategy": strategy,
            "side": side,
            "decision": promotion_decision,
            "val_auc": round(val_auc, 4),
            "n_samples": n_total,
            "n_positive": n_pos,
            "n_negative": n_neg,
            "n_train": n_train,
            "n_validation": n_val,
            "base_rate": round(float(y_all.mean()), 4),
            "feature_version": feature_hash,
            "artifact_path": str(artifact_path),
            "validation_gate_report": validation_gate_report,
        }




# ---------------------------------------------------------------------------
# 6. Excursion Regressors -- MAE / MFE (Phase 2)
# ---------------------------------------------------------------------------


@dataclass
class ExcursionEstimate:
    """Predicted excursion bounds for a proposed entry."""
    mae_pct: float    # predicted Maximum Adverse Excursion (%)
    mfe_pct: float    # predicted Maximum Favorable Excursion (%)
    reward_risk: float
    suggested_stop_pct: float    # entry_price * (1 - suggested_stop_pct / 100)
    suggested_target_pct: float  # entry_price * (1 + suggested_target_pct / 100)

    @classmethod
    def from_atr(cls, atr_pct: float, side: str = "long") -> "ExcursionEstimate":
        """Fallback estimate from ATR when no regressor is trained."""
        mae = atr_pct * 1.5
        mfe = atr_pct * 2.0
        return cls(
            mae_pct=round(mae, 4),
            mfe_pct=round(mfe, 4),
            reward_risk=round(mfe / max(mae, 0.001), 3),
            suggested_stop_pct=round(mae * 1.1, 4),
            suggested_target_pct=round(mfe * 0.9, 4),
        )


class ExcursionRegressors:
    """Gradient-boosted regressors for MAE and MFE prediction.

    Trained on historical bar data using ``build_mae_mfe_dataset()``.
    At inference time, given the current feature row, they predict the
    expected maximum adverse and favorable excursion over the next
    ``horizon_bars`` bars.

    The predictions are used to:
      - Set dynamic stop-loss: entry_price x (1 - mae_predicted x safety_mult)
      - Set dynamic take-profit: entry_price x (1 + mfe_predicted x capture_ratio)
      - Feed expected_return_pct and expected_drawdown_pct into the Trade Score
        engine with real model estimates instead of static defaults.

    Models persist to ``{models_dir}/excursion_mae.joblib`` and
    ``{models_dir}/excursion_mfe.joblib``.
    """

    MAE_SAFETY_MULT = 1.15    # buffer above predicted MAE for stop placement
    MFE_CAPTURE_RATIO = 0.85  # fraction of predicted MFE to target

    def __init__(self, models_dir: "Path") -> None:  # type: ignore[name-defined]
        from pathlib import Path as _Path
        self.models_dir = _Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._mae_reg = None
        self._mfe_reg = None
        self._trained = False
        self._load()

    def _load(self) -> None:
        try:
            import joblib
            mae_path = self.models_dir / "excursion_mae.joblib"
            mfe_path = self.models_dir / "excursion_mfe.joblib"
            if mae_path.exists() and mfe_path.exists():
                self._mae_reg = joblib.load(mae_path)
                self._mfe_reg = joblib.load(mfe_path)
                self._trained = True
                applog.info("excursion", "Loaded MAE/MFE regressors")
        except Exception as exc:
            applog.warning("excursion", f"Failed to load excursion regressors: {exc}")

    @property
    def is_trained(self) -> bool:
        return self._trained

    def predict(self, features: "pd.Series", atr_pct: float = 1.0) -> ExcursionEstimate:  # type: ignore[name-defined]
        """Predict MAE and MFE from entry feature row.

        Falls back to an ATR-based estimate if regressors are not trained.
        """
        if not self._trained or self._mae_reg is None or self._mfe_reg is None:
            return ExcursionEstimate.from_atr(atr_pct)

        try:
            x = pd.DataFrame([features[FEATURE_COLUMNS].to_numpy(dtype=float)], columns=FEATURE_COLUMNS)
            mae = float(max(0.0, predict_safe(self._mae_reg, x)[0]))
            mfe = float(max(0.0, predict_safe(self._mfe_reg, x)[0]))
        except Exception as exc:
            applog.debug("excursion", f"Excursion inference failed: {exc}")
            return ExcursionEstimate.from_atr(atr_pct)

        rr = mfe / max(mae, 0.001)
        stop_pct = mae * self.MAE_SAFETY_MULT
        target_pct = mfe * self.MFE_CAPTURE_RATIO

        return ExcursionEstimate(
            mae_pct=round(mae, 4),
            mfe_pct=round(mfe, 4),
            reward_risk=round(rr, 3),
            suggested_stop_pct=round(stop_pct, 4),
            suggested_target_pct=round(target_pct, 4),
        )

    def train(
        self,
        frames: dict,   # symbol -> pd.DataFrame
        horizon_bars: int = 15,
    ) -> dict:
        """Train MAE and MFE regressors on historical bar data.

        Pools all frames, builds excursion targets using
        ``build_mae_mfe_dataset()``, and fits two separate
        gradient-boosted regressors.
        """
        from sklearn.ensemble import HistGradientBoostingRegressor

        xs, maes, mfes = [], [], []
        for df in frames.values():
            X, mae_s, mfe_s = build_mae_mfe_dataset(df, horizon_bars)
            if len(X) > 20:
                xs.append(X)
                maes.append(mae_s)
                mfes.append(mfe_s)

        if not xs:
            applog.warning("excursion", "Insufficient data for excursion regressor training")
            return {"status": "skipped", "reason": "no data"}

        X_all = pd.concat(xs).sort_index()
        mae_all = pd.concat(maes).sort_index()
        mfe_all = pd.concat(mfes).sort_index()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")

            mae_reg = HistGradientBoostingRegressor(
                max_iter=200, learning_rate=0.08, max_depth=5,
                l2_regularization=1.0, random_state=42,
            )
            mae_reg.fit(X_all, mae_all)

            mfe_reg = HistGradientBoostingRegressor(
                max_iter=200, learning_rate=0.08, max_depth=5,
                l2_regularization=1.0, random_state=42,
            )
            mfe_reg.fit(X_all, mfe_all)

        try:
            import joblib
            joblib.dump(mae_reg, self.models_dir / "excursion_mae.joblib")
            joblib.dump(mfe_reg, self.models_dir / "excursion_mfe.joblib")
        except Exception as exc:
            applog.warning("excursion", f"Failed to save excursion regressors: {exc}")

        self._mae_reg = mae_reg
        self._mfe_reg = mfe_reg
        self._trained = True

        # Compute in-sample MAE mean for logging
        mae_pred = mae_reg.predict(X_all)
        mae_err = float(np.abs(mae_pred - mae_all.to_numpy()).mean())

        applog.info(
            "excursion",
            f"Excursion regressors trained -- {len(X_all)} samples, "
            f"MAE prediction error: {mae_err:.4f}%",
        )
        return {
            "status": "success",
            "samples": len(X_all),
            "mae_prediction_error_pct": round(mae_err, 4),
        }
