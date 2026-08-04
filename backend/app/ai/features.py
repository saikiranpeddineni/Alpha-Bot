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
]

SESSION_MINUTES = 390


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Append feature columns to an (enriched) bar frame."""
    if df.empty:
        return df.copy()
    out = df if is_enriched(df) else enrich(df)
    out = out.copy()

    close = out["close"]
    out["macd_hist_norm"] = out["macd_hist"] / close * 1000.0
    out["atr_pct"] = out["atr14"] / close * 100.0
    out["price_velocity"] = (close - close.shift(3)) / out["atr14"].replace(0.0, np.nan)

    et_index = out.index.tz_convert(ET)
    minute = (et_index.hour * 60 + et_index.minute) - (9 * 60 + 30)
    minute = np.clip(minute, 0, SESSION_MINUTES - 1)
    out["minute_sin"] = np.sin(2.0 * np.pi * minute / SESSION_MINUTES)
    out["minute_cos"] = np.cos(2.0 * np.pi * minute / SESSION_MINUTES)
    out["day_progress"] = minute / SESSION_MINUTES
    return out


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
) -> tuple[pd.DataFrame, pd.Series]:
    """Supervised dataset: features at bar t, label = forward return over the
    next `horizon_bars` exceeding `threshold_pct` (cost-adjusted edge) for long,
    or falling below `-threshold_pct` for short."""
    feats = compute_features(df)
    if feats.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS), pd.Series(dtype=int)
    fwd_ret = feats["close"].shift(-horizon_bars) / feats["close"] - 1.0

    # Purge forward returns that cross a calendar day.
    # F-05 FIX: Both sides explicitly cast to numpy arrays to avoid silent
    # pandas/numpy broadcast misalignment when comparing DatetimeIndex.date
    # (numpy array) against a shifted dt.date Series (pandas Series).
    et_dates = np.array(feats.index.tz_convert(ET).date)
    et_series = pd.Series(feats.index.tz_convert(ET), index=feats.index)
    shifted_et_dates = np.array(et_series.shift(-horizon_bars).dt.date)
    mask_same_day = et_dates == shifted_et_dates
    fwd_ret.loc[~mask_same_day] = np.nan

    if side == "short":
        label = (fwd_ret * 100.0 < -threshold_pct).astype(int)
    else:
        label = (fwd_ret * 100.0 > threshold_pct).astype(int)

    X = feats[FEATURE_COLUMNS]
    mask = ~(X.isna().any(axis=1) | fwd_ret.isna())
    return X[mask], label[mask]


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

    close = feats["close"].to_numpy()
    n = len(close)
    mae = np.full(n, np.nan)
    mfe = np.full(n, np.nan)
    et_dates = feats.index.tz_convert(ET).date

    for i in range(n - horizon_bars):
        entry = close[i]
        if entry <= 0 or et_dates[i] != et_dates[i + horizon_bars]:
            continue
        window = close[i + 1 : i + 1 + horizon_bars]
        mae[i] = float(max(0.0, (entry - window.min()) / entry * 100.0))
        mfe[i] = float(max(0.0, (window.max() - entry) / entry * 100.0))

    X = feats[FEATURE_COLUMNS]
    mae_s = pd.Series(mae, index=feats.index)
    mfe_s = pd.Series(mfe, index=feats.index)
    mask = ~(X.isna().any(axis=1) | mae_s.isna() | mfe_s.isna())
    return X[mask], mae_s[mask], mfe_s[mask]
