"""Vectorized technical indicators (pandas/numpy only, no native deps).

Bar DataFrame convention used across the platform:
  index   : tz-aware UTC DatetimeIndex (bar open time)
  columns : open, high, low, close, volume

`enrich()` appends the standard indicator set consumed by strategies and the
AI feature pipeline. It is idempotent and safe on short frames (NaN warmup).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

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
    width = (upper - lower) / mid
    # Position of price within the bands: 0 = lower band, 1 = upper band
    span = (upper - lower).replace(0.0, np.nan)
    pos = (series - lower) / span
    return pd.DataFrame(
        {"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width, "bb_pos": pos}
    )


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price anchored to each U.S. trading session (ET date)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float)
    session = df.index.tz_convert(ET).date
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


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Append the standard indicator column set. Returns a new DataFrame."""
    if df.empty:
        return df.copy()
    out = df.copy()
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
    out = out.join(bollinger(close, 20, 2.0))
    out["vwap"] = session_vwap(out)
    out["vwap_dist_pct"] = (close / out["vwap"] - 1.0) * 100.0
    out["vol_sma20"] = sma(out["volume"].astype(float), 20)
    out["vol_z"] = rolling_zscore(out["volume"].astype(float), 20)
    out["er20"] = efficiency_ratio(close, 20)
    out["ret_1"] = close.pct_change(1)
    out["ret_5"] = close.pct_change(5)
    out["ret_15"] = close.pct_change(15)
    return out


REQUIRED_ENRICHED_COLUMNS = (
    "ema9", "ema20", "ema50", "rsi14", "rsi2", "macd_hist", "atr14",
    "bb_pos", "bb_width", "vwap", "vwap_dist_pct", "vol_z", "er20",
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
    # Weighted blend: 40% 5-min, 60% 15-min (smoother than 5m alone)
    rs_raw = 0.40 * (s_ret5 - b_ret5) + 0.60 * (s_ret15 - b_ret15)
    # Normalize: 0.0 center at SPY parity, saturate at ±2% differential → 0.0–1.0
    return max(0.0, min(1.0, rs_raw / 2.0 + 0.5))
