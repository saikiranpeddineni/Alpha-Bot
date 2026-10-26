"""Vectorized technical indicators (pandas/numpy only, no native deps).

Bar DataFrame convention used across the platform:
  index   : tz-aware UTC DatetimeIndex (bar open time)
  columns : open, high, low, close, volume

`enrich()` appends the standard indicator set consumed by strategies and the
AI feature pipeline. It is idempotent and safe on short frames (NaN warmup).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # When avg_loss == 0 (straight-up move), RSI is 100 by definition.
    out = out.where(avg_loss != 0.0, 100.0)
    out[avg_gain.isna() | avg_loss.isna()] = np.nan
    return out


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    fast_ema = series.ewm(span=fast, adjust=False).mean()
    slow_ema = series.ewm(span=slow, adjust=False).mean()
    line = fast_ema - slow_ema
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR."""
    return true_range(df).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(series, period)
    std = series.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    # =============================================================================
    # [MODIFIED 2026-10-14]: L48 - Bollinger Width Zero-Division Guard
    # ORIGINAL INTENT: (upper - lower) / mid computed Bollinger band width without zero check.
    # REASON FOR CHANGE: If mid == 0.0, division by zero raises warnings/inf.
    # SAFE ROLLBACK: The original code is removed; rollback via git history.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    width = (upper - lower) / mid.replace(0.0, np.nan)
    # =============================================================================
    span = (upper - lower).replace(0.0, np.nan)
    pos = (series - lower) / span
    return pd.DataFrame(
        {"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width, "bb_pos": pos}
    )


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price anchored to each U.S. trading session (ET date)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series(1.0, index=df.index)
    if getattr(df.index, "tz", None) is not None:
        session = df.index.tz_convert(ET).date
    else:
        session = df.index.date
    pv_cum = (typical * vol).groupby(session).cumsum()
    v_cum = vol.groupby(session).cumsum().replace(0.0, np.nan)
    return pv_cum / v_cum


def rolling_zscore(series: pd.Series, period: int = 20) -> pd.Series:
    mean = series.rolling(period, min_periods=period).mean()
    std = series.rolling(period, min_periods=period).std(ddof=0).replace(0.0, np.nan)
    return (series - mean) / std


def efficiency_ratio(series: pd.Series, period: int = 20) -> pd.Series:
    """Kaufman efficiency ratio: |net change| / sum(|bar changes|). 1 = perfect trend, 0 = noise."""
    change = (series - series.shift(period)).abs()
    volatility = series.diff().abs().rolling(period, min_periods=period).sum()
    return change / volatility.replace(0.0, np.nan)


def volatility_expansion(df: pd.DataFrame, short_period: int = 5, long_period: int = 20) -> pd.Series:
    """Ratio of short-term ATR to long-term ATR to measure sudden panic/expansion."""
    short_atr = atr(df, short_period)
    long_atr = atr(df, long_period)
    return short_atr / long_atr.replace(0.0, np.nan)


def tail_whipsaw(df: pd.DataFrame) -> pd.Series:
    """Ratio of wick size to total candle size. Higher means more fake-outs."""
    body_size = (df["open"] - df["close"]).abs()
    total_size = df["high"] - df["low"]
    whipsaw_size = total_size - body_size
    return whipsaw_size / total_size.replace(0.0, np.nan)


def opening_range_distance(df: pd.DataFrame, range_minutes: int = 15) -> pd.Series:
    """Distance from the first N-minute opening range high/low. Positive means breakout, negative means inside range."""
    if df.empty:
        return pd.Series(dtype=float)

    out = pd.Series(index=df.index, dtype=float)
    et_index = df.index.tz_convert(ET) if getattr(df.index, "tz", None) is not None else df.index

    # Extract time as minutes since midnight ET
    mins_et = et_index.hour * 60 + et_index.minute

    # 9:30 AM is 570 minutes
    is_or_bar = (mins_et >= 570) & (mins_et < 570 + range_minutes)

    sessions = et_index.date
    for session in pd.unique(sessions):
        session_mask = (sessions == session)
        or_mask = session_mask & is_or_bar

        if not or_mask.any():
            continue

        or_high = float(df.loc[or_mask, "high"].max())
        or_low = float(df.loc[or_mask, "low"].min())

        # We only calculate distance for bars AFTER the opening range is established
        post_or_mask = session_mask & (mins_et >= 570 + range_minutes)
        if post_or_mask.any():
            close = df.loc[post_or_mask, "close"].astype(float)
            # =============================================================================
            # [MODIFIED 2026-10-14]: L48 - Opening Range Zero-Division Guard
            # ORIGINAL INTENT: (close - or_high) / or_high computed distance without zero check.
            # REASON FOR CHANGE: If or_high or or_low is 0.0, division by zero raises warnings/inf.
            # SAFE ROLLBACK: The original code is removed; rollback via git history.
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            safe_or_high = or_high if abs(or_high) > 1e-9 else np.nan
            safe_or_low = or_low if abs(or_low) > 1e-9 else np.nan
            dist_up = (close - or_high) / safe_or_high
            dist_down = (or_low - close) / safe_or_low
            # =============================================================================
            out.loc[post_or_mask] = pd.concat([dist_up, dist_down], axis=1).max(axis=1) * 100.0

    # For bars before OR is established, fill with 0
    return out.fillna(0.0)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Append the standard indicator column set. Returns a new DataFrame."""
    if df is None or len(df) == 0:
        return df.copy() if hasattr(df, 'copy') else df
    # Idempotency fast-path: pre-enriched frames (replay pre-computes the full
    # history once) pass through untouched instead of recomputing ~20 rolling
    # operations per call. Note: a *view* of an enriched frame is enriched.
    if is_enriched(df):
        return df
    # =============================================================================
    # [MODIFIED 2026-10-26]: Deduplicate DatetimeIndex in enrich
    # ORIGINAL INTENT: Created a copy of df directly and performed indicator joins.
    # REASON FOR CHANGE: Duplicate timestamps in raw bar frames cause downstream join
    # operations (MACD, Bollinger Bands) to crash with duplicate label errors or produce
    # cartesian products. Deduplicating by keeping the latest bar prevents crashes across all consumers.
    # SAFE ROLLBACK: The original code is preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    #     out = df.copy()
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    out = df[~df.index.duplicated(keep="last")].copy() if df.index.has_duplicates else df.copy()
    # =============================================================================
    for col in ("open", "high", "low", "close", "volume"):
        if col in out.columns:
            out[col] = out[col].astype(float)
    close = out["close"]
    out["ema9"] = ema(close, 9)
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)
    out["rsi14"] = rsi(close, 14)
    out["rsi2"] = rsi(close, 2)
    out = out.join(macd(close))
    out["atr14"] = atr(out, 14)
    out["atr_ext"] = (close - out["ema9"]).abs() / out["atr14"].replace(0.0, 0.001)
    out = out.join(bollinger(close, 20, 2.0))
    out["vwap"] = session_vwap(out)
    safe_vwap = out["vwap"].replace(0.0, np.nan)
    out["vwap_dist_pct"] = (close / safe_vwap - 1.0) * 100.0
    out["vol_sma20"] = sma(out["volume"].astype(float), 20)
    out["vol_z"] = rolling_zscore(out["volume"].astype(float), 20)
    out["er20"] = efficiency_ratio(close, 20)
    out["vol_expansion"] = volatility_expansion(out, 5, 20)
    out["whipsaw_ratio"] = tail_whipsaw(out)
    out["or_dist_pct"] = opening_range_distance(out, 15)
    out["ret_1"] = close.pct_change(1)
    out["ret_5"] = close.pct_change(5)
    out["ret_15"] = close.pct_change(15)
    return out


REQUIRED_ENRICHED_COLUMNS = (
    "ema9", "ema20", "ema50", "rsi14", "rsi2", "macd_hist", "atr14", "atr_ext",
    "bb_pos", "bb_width", "vwap", "vwap_dist_pct", "vol_z", "er20",
    "vol_expansion", "whipsaw_ratio", "or_dist_pct",
)


def is_enriched(df: pd.DataFrame) -> bool:
    return all(c in df.columns for c in REQUIRED_ENRICHED_COLUMNS)


def compute_relative_strength(symbol_df: pd.DataFrame, spy_df: pd.DataFrame) -> float | None:
    """Calculate Relative Strength vs SPY — multi-timeframe (40% ret_5 + 60% ret_15)."""
    if (
        symbol_df.empty or spy_df.empty
        or "ret_5" not in symbol_df.columns or "ret_15" not in symbol_df.columns
        or "ret_5" not in spy_df.columns or "ret_15" not in spy_df.columns
    ):
        return None
    s_ret5 = float(symbol_df["ret_5"].iloc[-1])
    s_ret15 = float(symbol_df["ret_15"].iloc[-1])
    b_ret5 = float(spy_df["ret_5"].iloc[-1])
    b_ret15 = float(spy_df["ret_15"].iloc[-1])
    if pd.isna(s_ret5) or pd.isna(s_ret15) or pd.isna(b_ret5) or pd.isna(b_ret15):
        return None
    # Weighted blend: 40% 5-min, 60% 15-min (smoother than 5m alone)
    rs_raw = 0.40 * (s_ret5 - b_ret5) + 0.60 * (s_ret15 - b_ret15)
    # Normalize: 0.0 center at SPY parity, saturate at ±2% differential → 0.0–1.0
    return max(0.0, min(1.0, rs_raw / 2.0 + 0.5))
