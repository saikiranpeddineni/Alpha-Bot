"""Feature engineering for the ML signal model.

Features are derived from the standard enriched indicator frame so the exact
same computation feeds live inference, backtests and training -- no
train/serve skew.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from app.indicators.ta import enrich, is_enriched

ET = ZoneInfo("America/New_York")

FEATURE_COLUMNS = [
    "ret_1",
    "ret_5",
    "ret_15",
    "rsi14",
    "rsi2",
    "macd_hist_norm",
    "atr_pct",
    "vol_z",
    "vwap_dist_pct",
    "bb_pos",
    "bb_width",
    "er20",
    "vol_expansion",
    "whipsaw_ratio",
    "price_velocity",
    "minute_sin",
    "minute_cos",
    "day_progress",
    "regime_val",
]

SESSION_MINUTES = 390


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Append feature columns to an (enriched) bar frame."""
    if df.empty:
        return df
    if "regime_val" in df.columns:
        return df
        
    out = df if is_enriched(df) else enrich(df)
    # We deliberately mutate the dataframe directly to cache features across training loops
    out["macd_hist_norm"] = out["macd_hist"] / out["close"] * 1000.0

    out["atr_pct"] = out["atr14"] / out["close"] * 100.0
    out["price_velocity"] = (out["close"] - out["close"].shift(3)) / out["atr14"].replace(0.0, np.nan)

    et_index = out.index.tz_convert(ET)
    minute = (et_index.hour * 60 + et_index.minute) - (9 * 60 + 30)
    minute = np.clip(minute, 0, SESSION_MINUTES - 1)
    out["minute_sin"] = np.sin(2.0 * np.pi * minute / SESSION_MINUTES)
    out["minute_cos"] = np.cos(2.0 * np.pi * minute / SESSION_MINUTES)
    out["day_progress"] = minute / SESSION_MINUTES

    # Regime calculation mapped to integers for ML (0=RANGE, 1=TREND_UP, -1=TREND_DOWN, 2=VOLATILE)
    er20 = out.get("er20", pd.Series(0, index=out.index)).fillna(0)
    ema20 = out.get("ema20", pd.Series(np.nan, index=out.index))
    slope = (ema20 / ema20.shift(10) - 1.0) * 100.0
    vol_p85 = out["atr_pct"].rolling(390, min_periods=1).quantile(0.85)

    regime_val = pd.Series(0.0, index=out.index)
    mask_volatile = out["atr_pct"] >= vol_p85
    mask_trend_up = (~mask_volatile) & (er20 >= 0.30) & (slope > 0)
    mask_trend_down = (~mask_volatile) & (er20 >= 0.30) & (slope <= 0)

    regime_val.loc[mask_trend_up] = 1.0
    regime_val.loc[mask_trend_down] = -1.0
    regime_val.loc[mask_volatile] = 2.0
    out["regime_val"] = regime_val

    df[out.columns] = out
    return df


def latest_feature_row(df: pd.DataFrame) -> pd.Series | None:
    """Feature vector for the most recent bar, or None during warmup."""
    feats = compute_features(df)
    if feats.empty:
        return None
    row = feats.iloc[-1]
    values = row[FEATURE_COLUMNS]
    if values.isna().any():
        return None
    return values.astype(float)


def build_training_set(
    df: pd.DataFrame,
    horizon_bars: int = 15,
    threshold_pct: float = 0.10,
    side: str = "long",
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Supervised dataset: features at bar t, label = forward return over the
    next `horizon_bars` exceeding dynamic ATR-scaled threshold.
    
    Returns:
        X (DataFrame): Features
        y (Series): Binary label
        w (Series): Sample weights scaled by profit magnitude to maximize recall
    """
    feats = compute_features(df)
    if feats.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS), pd.Series(dtype=int), pd.Series(dtype=float)
    fwd_ret = feats["close"].shift(-horizon_bars) / feats["close"] - 1.0

    # Dynamic Volatility Threshold: 50% of the daily ATR (approx)
    # Using the precomputed `atr_pct` which is ATR14 / Close * 100.
    # We clip it so it never drops below 0.05% (to avoid micro-noise) and never exceeds 0.50% (to avoid missing valid trades in extreme chop).
    dyn_threshold = (feats["atr_pct"] * 0.5).clip(lower=0.05, upper=0.50)

    # Purge forward returns that cross a calendar day.
    et_dates = np.array(feats.index.tz_convert(ET).date)
    et_series = pd.Series(feats.index.tz_convert(ET), index=feats.index)
    shifted_et_dates = np.array(et_series.shift(-horizon_bars).dt.date)
    mask_same_day = et_dates == shifted_et_dates
    fwd_ret.loc[~mask_same_day] = np.nan

    if side == "short":
        label = (fwd_ret * 100.0 < -dyn_threshold).astype(int)
    else:
        label = (fwd_ret * 100.0 > dyn_threshold).astype(int)

    # Asymmetric Profit Weights: heavily penalize false negatives for highly profitable trades
    # Base weight is 1.0. For every 0.10% of profit, we add weight.
    # e.g., a 1.0% profit trade gets a weight of ~11.0, forcing the tree to capture it.
    fwd_ret_pct = fwd_ret * 100.0
    if side == "short":
        profit_magnitude = np.clip(-fwd_ret_pct, 0.0, 5.0)
    else:
        profit_magnitude = np.clip(fwd_ret_pct, 0.0, 5.0)
    
    # Give winning trades exponential weight so big winners are NEVER missed
    weight = 1.0 + (profit_magnitude * 10.0)

    X = feats[FEATURE_COLUMNS]
    mask = ~(X.isna().any(axis=1) | fwd_ret.isna() | dyn_threshold.isna())
    return X[mask], label[mask], weight[mask]


def build_mae_mfe_dataset(
    df: pd.DataFrame,
    horizon_bars: int = 15,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Build regression targets for Maximum Adverse Excursion (MAE) and
    Maximum Favorable Excursion (MFE) over the next ``horizon_bars``.

    MAE: worst unrealised loss as a % of entry price (always positive).
    MFE: best unrealised gain as a % of entry price (always positive).

    These are used to train the ExcursionRegressors so that stop-loss and
    take-profit levels can be set from predicted excursion distributions
    rather than static ATR multiples.

    Returns
    -------
    X        : feature DataFrame (FEATURE_COLUMNS)
    mae_pct  : MAE in % (regression target, ≥ 0)
    mfe_pct  : MFE in % (regression target, ≥ 0)
    """
    feats = compute_features(df)
    if feats.empty or len(feats) <= horizon_bars:
        empty = pd.DataFrame(columns=FEATURE_COLUMNS)
        return empty, pd.Series(dtype=float), pd.Series(dtype=float)

    close_s = feats["close"]
    
    # Vectorized forward window
    indexer = pd.api.indexers.FixedForwardWindowIndexer(window_size=horizon_bars)
    shifted_close = close_s.shift(-1)
    window_min = shifted_close.rolling(indexer).min()
    window_max = shifted_close.rolling(indexer).max()

    # Temporal purity check
    et_series = pd.Series(feats.index.tz_convert(ET), index=feats.index)
    et_dates = et_series.dt.date
    shifted_et_dates = et_series.shift(-horizon_bars).dt.date

    mask_valid = (close_s > 0) & (et_dates == shifted_et_dates)
    
    mae_pct = ((close_s - window_min) / close_s * 100.0).clip(lower=0.0)
    mfe_pct = ((window_max - close_s) / close_s * 100.0).clip(lower=0.0)

    mae_pct.loc[~mask_valid] = np.nan
    mfe_pct.loc[~mask_valid] = np.nan

    X = feats[FEATURE_COLUMNS]
    mask = ~(X.isna().any(axis=1) | mae_pct.isna() | mfe_pct.isna())
    return X[mask], mae_pct[mask], mfe_pct[mask]
