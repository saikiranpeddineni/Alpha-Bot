from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.domain import Regime, SignalAction
from app.strategies.base import StrategyContext
from app.strategies.gap_and_go import GapAndGo
from app.strategies.trend_pullback import TrendPullback
from app.strategies.liquidity_sweep import LiquiditySweep
from app.strategies.relative_strength import RelativeStrength


ET = ZoneInfo("America/New_York")


def _frame(rows):
    index = pd.DatetimeIndex([r.pop("ts") for r in rows]).tz_convert(ET)
    return pd.DataFrame(rows, index=index)


def test_gap_and_go_uses_dynamic_reward_risk_and_gap_fallback():
    df = _frame([
        {"ts": datetime(2026, 8, 7, 9, 30, tzinfo=ET), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "vwap": 100.0, "ema9": 100.0, "ema20": 100.0, "vol_z": 1.0},
        {"ts": datetime(2026, 8, 10, 10, 0, tzinfo=ET), "open": 102.0, "high": 106.0, "low": 101.5, "close": 105.0, "vwap": 103.0, "ema9": 101.0, "ema20": 100.0, "vol_z": 2.0},
    ])
    strat = GapAndGo(params={"reward_risk": 3.0, "stop_buffer": 0.10})
    ctx = StrategyContext(position=None, equity=100_000, regime=Regime.TREND_UP, now=df.index[-1].to_pydatetime())

    sig = strat.on_bar(ctx, "TEST", df)

    assert sig is not None
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.stop_loss == 99.9
    assert sig.take_profit == 120.0


def test_trend_pullback_long_and_short_use_dynamic_geometry():
    long_df = _frame([
        {"ts": datetime(2026, 8, 10, 10, 0, tzinfo=ET), "open": 99.8, "high": 100.6, "low": 99.0, "close": 100.4,
         "er20": 0.60, "ema20": 100.0, "ema50": 99.0, "rsi14": 50.0, "vol_z": 0.0, "atr14": 2.0},
    ])
    long_strat = TrendPullback(params={"reward_risk": 4.0, "value_zone_atr_mult": 0.75})
    ctx = StrategyContext(position=None, equity=100_000, regime=Regime.TREND_UP, now=long_df.index[-1].to_pydatetime())
    long_sig = long_strat.on_bar(ctx, "TEST", long_df)

    assert long_sig is not None
    assert long_sig.action == SignalAction.ENTER_LONG
    assert long_sig.stop_loss == 97.5
    assert long_sig.take_profit == 112.0

    short_df = _frame([
        {"ts": datetime(2026, 8, 10, 10, 0, tzinfo=ET), "open": 99.2, "high": 100.0, "low": 98.4, "close": 98.6,
         "er20": 0.60, "ema20": 99.0, "ema50": 100.0, "rsi14": 50.0, "vol_z": 0.0, "atr14": 2.0},
    ])
    short_strat = TrendPullback(params={"reward_risk": 4.0, "value_zone_atr_mult": 0.75})
    ctx = StrategyContext(position=None, equity=100_000, regime=Regime.TREND_DOWN, now=short_df.index[-1].to_pydatetime())
    short_sig = short_strat.on_bar(ctx, "TEST", short_df)

    assert short_sig is not None
    assert short_sig.action == SignalAction.ENTER_SHORT
    assert short_sig.stop_loss == 101.5
    assert short_sig.take_profit == 87.4


def test_liquidity_sweep_signal_path():
    rows = []
    for i in range(30):
        rows.append({
            "ts": datetime(2026, 8, 10, 10, 0, tzinfo=ET).replace(minute=10 + i),
            "open": 100.0,
            "high": 100.0,
            "low": 99.0,
            "close": 99.5,
            "vol_z": 2.0,
            "whipsaw_ratio": 0.60,
        })
    rows[-1].update({"open": 100.0, "high": 100.2, "low": 99.4, "close": 99.5})
    df = _frame(rows)
    strat = LiquiditySweep()
    ctx = StrategyContext(position=None, equity=100_000, regime=Regime.RANGE, now=df.index[-1].to_pydatetime())

    sig = strat.on_bar(ctx, "TEST", df)

    assert sig is not None
    assert sig.action == SignalAction.ENTER_SHORT


def test_relative_strength_signal_path():
    rows = []
    for i in range(30):
        rows.append({
            "ts": datetime(2026, 8, 10, 10, 0, tzinfo=ET).replace(minute=10 + i),
            "open": 99.0,
            "high": 100.0,
            "low": 98.5,
            "close": 99.5,
            "vol_z": 2.0,
        })
    rows[-1].update({"close": 101.0, "open": 100.5, "high": 101.2})
    df = _frame(rows)
    strat = RelativeStrength(params={"rs_long_threshold": 0.75, "vol_z_min": 1.2})
    ctx = StrategyContext(
        position=None,
        equity=100_000,
        regime=Regime.TREND_UP,
        now=df.index[-1].to_pydatetime(),
        rs_score=0.80,
    )

    sig = strat.on_bar(ctx, "TEST", df)

    assert sig is not None
    assert sig.action == SignalAction.ENTER_LONG
