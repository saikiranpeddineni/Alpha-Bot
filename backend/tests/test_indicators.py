"""Indicator correctness tests (known values and invariants)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators.ta import (
    atr,
    bollinger,
    efficiency_ratio,
    ema,
    enrich,
    is_enriched,
    macd,
    rolling_zscore,
    rsi,
    session_vwap,
    sma,
)
from tests.conftest import make_bars


def test_sma_known_values():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(s, 3)
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == 2.0
    assert out.iloc[4] == 4.0


def test_ema_converges_to_constant():
    s = pd.Series([10.0] * 50)
    out = ema(s, 9)
    assert abs(out.iloc[-1] - 10.0) < 1e-9


def test_rsi_extremes():
    up = pd.Series(np.linspace(100, 120, 40))
    down = pd.Series(np.linspace(120, 100, 40))
    assert rsi(up, 14).iloc[-1] > 99.0  # straight up -> RSI ~ 100
    assert rsi(down, 14).iloc[-1] < 1.0
    mixed = pd.Series(100 + np.sin(np.arange(60) / 3.0))
    val = rsi(mixed, 14).iloc[-1]
    assert 0.0 <= val <= 100.0


def test_macd_signs():
    rising = pd.Series(np.linspace(100, 130, 100))
    out = macd(rising)
    assert out["macd"].iloc[-1] > 0
    falling = pd.Series(np.linspace(130, 100, 100))
    assert macd(falling)["macd"].iloc[-1] < 0


def test_atr_positive_and_scales():
    calm = make_bars(100, vol=0.0005, seed=1)
    wild = make_bars(100, vol=0.005, seed=1)
    a_calm = atr(calm, 14).iloc[-1]
    a_wild = atr(wild, 14).iloc[-1]
    assert a_calm > 0
    assert a_wild > a_calm * 2


def test_bollinger_bands_order_and_position():
    df = make_bars(100)
    out = bollinger(df["close"], 20, 2.0)
    last = out.dropna().iloc[-1]
    assert last["bb_lower"] < last["bb_mid"] < last["bb_upper"]


def test_session_vwap_resets_each_session():
    d1 = make_bars(390, start=pd.Timestamp("2026-01-06 14:30", tz="UTC"), price=100.0, seed=2)
    d2 = make_bars(390, start=pd.Timestamp("2026-01-07 14:30", tz="UTC"), price=200.0, seed=3)
    df = pd.concat([d1, d2])
    v = session_vwap(df)
    # First bar of day 2 must anchor near day-2 prices, not blend with day 1
    first_d2 = v.iloc[390]
    assert 150.0 < first_d2 < 260.0
    typical_first = (d2["high"].iloc[0] + d2["low"].iloc[0] + d2["close"].iloc[0]) / 3.0
    assert abs(first_d2 - typical_first) < 1e-6


def test_rolling_zscore_centered():
    s = pd.Series(np.sin(np.arange(200) / 5.0) * 10 + 100)
    z = rolling_zscore(s, 50).dropna()
    assert abs(z.mean()) < 0.5


def test_efficiency_ratio_bounds():
    trend = pd.Series(np.linspace(100, 110, 50))
    noise = pd.Series(100 + np.sin(np.arange(50)) * 2)
    er_trend = efficiency_ratio(trend, 20).iloc[-1]
    er_noise = efficiency_ratio(noise, 20).iloc[-1]
    assert er_trend > 0.99  # perfect trend
    assert er_noise < 0.3


def test_enrich_adds_all_columns_and_is_idempotent():
    df = make_bars(120)
    out = enrich(df)
    assert is_enriched(out)
    for col in ("ema9", "rsi14", "atr14", "vwap", "vol_z", "er20", "macd_hist", "bb_pos"):
        assert col in out.columns
        assert pd.notna(out[col].iloc[-1])
    assert len(out) == len(df)
    assert not is_enriched(df)  # original untouched


def test_enrich_empty_frame():
    from app.data.base import empty_bars

    out = enrich(empty_bars())
    assert out.empty
