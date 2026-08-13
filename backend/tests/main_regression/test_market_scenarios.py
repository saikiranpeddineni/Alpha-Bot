from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.domain import Regime, SignalAction, Signal
from app.strategies.entry_intelligence import assess_entry_context


def _signal(action, **kwargs):
    base = dict(
        action=action,
        side="long" if action == SignalAction.ENTER_LONG else "short",
        strategy="momentum",
        symbol="TEST",
        mover_bias=None,
        mover_change_pct=0.0,
        rs_score=None,
        news_context_available=False,
        news_direction="neutral",
        news_sentiment=0.0,
        news_confidence=0.0,
        news_catalyst=None,
        news_age_seconds=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _bars(closes, ema9=None, ema20=None):
    n = len(closes)
    df = pd.DataFrame(
        {
            "close": closes,
            "high": [x + 0.2 for x in closes],
            "low": [x - 0.2 for x in closes],
            "atr14": [1.0] * n,
        }
    )
    if ema9 is not None:
        df["ema9"] = [ema9] * n
    if ema20 is not None:
        df["ema20"] = [ema20] * n
    return df


def test_scenario_1_strong_uptrend():
    sig = _signal(SignalAction.ENTER_LONG, mover_bias="gainer")
    closes = [100.0 + i * 0.5 for i in range(30)]
    result = assess_entry_context(sig, _bars(closes, ema9=114.0, ema20=105.0), Regime.TREND_UP)
    assert result.allowed is True
    assert not result.reasons


def test_scenario_2_strong_downtrend():
    sig = _signal(SignalAction.ENTER_SHORT, mover_bias="loser")
    closes = [100.0 - i * 0.5 for i in range(30)]
    result = assess_entry_context(sig, _bars(closes, ema9=86.0, ema20=95.0), Regime.TREND_DOWN)
    assert result.allowed is True
    assert not result.reasons


def test_scenario_3_range_chop():
    sig_weak = _signal(SignalAction.ENTER_LONG, rs_score=0.5)
    closes_chop = [100.0, 100.2, 99.8, 100.1, 99.9] * 6
    result_weak = assess_entry_context(sig_weak, _bars(closes_chop, ema9=100.0, ema20=100.0), Regime.RANGE)
    assert result_weak.allowed is True
    assert any("macro_context_conflict" in r for r in result_weak.reasons)

    sig_strong = _signal(SignalAction.ENTER_LONG, mover_bias="gainer", rs_score=0.9)
    result_strong = assess_entry_context(sig_strong, _bars(closes_chop, ema9=100.0, ema20=100.0), Regime.RANGE)
    assert result_strong.allowed is True
    assert not any("macro_context_conflict" in r for r in result_strong.reasons)


def test_scenario_4_high_volatility():
    sig = _signal(SignalAction.ENTER_LONG, mover_bias="gainer")
    closes = [100.0, 105.0, 95.0, 108.0, 92.0, 110.0] * 5
    result = assess_entry_context(sig, _bars(closes, ema9=102.0, ema20=100.0), Regime.VOLATILE)
    assert result.allowed is True


def test_scenario_5_low_volatility():
    sig = _signal(SignalAction.ENTER_LONG, mover_bias="gainer")
    closes = [100.0, 100.05, 99.95, 100.1, 99.9] * 6
    result = assess_entry_context(sig, _bars(closes, ema9=100.0, ema20=100.0), Regime.RANGE)
    assert result.allowed is True


def test_scenario_6_gap_continuation():
    sig = _signal(SignalAction.ENTER_LONG, strategy="gap_and_go", mover_bias="gainer")
    closes = [100.0] * 10 + [110.0 + i * 0.1 for i in range(20)]
    result = assess_entry_context(sig, _bars(closes, ema9=111.0, ema20=105.0), Regime.TREND_UP)
    assert result.allowed is True


def test_scenario_7_gap_fade():
    sig = _signal(SignalAction.ENTER_SHORT, strategy="gap_and_go", mover_bias="gainer")
    closes = [100.0] * 10 + [110.0 - i * 0.2 for i in range(20)]
    result = assess_entry_context(sig, _bars(closes, ema9=108.0, ema20=109.0), Regime.TREND_UP)
    assert result.allowed is True


def test_scenario_8_false_breakout():
    sig = _signal(SignalAction.ENTER_SHORT, mover_bias="gainer")
    closes = [100.0 + i * 0.1 for i in range(15)] + [102.0 - i * 0.5 for i in range(15)]
    result = assess_entry_context(sig, _bars(closes, ema9=98.0, ema20=100.0), Regime.TREND_UP)
    assert result.allowed is True


def test_scenario_9_news_shock():
    sig = _signal(
        SignalAction.ENTER_LONG,
        mover_bias="gainer",
        news_context_available=True,
        news_direction="bearish",
        news_confidence=0.9,
        news_catalyst="earnings",
        news_age_seconds=120,
    )
    closes = [100.0] * 30  # Flat price so reaction is not absorbing the news
    result = assess_entry_context(sig, _bars(closes, ema9=102.0, ema20=100.0), Regime.TREND_UP)
    assert result.allowed is True
    assert any("news_context_conflict" in r for r in result.reasons)


def test_scenario_10_reversal():
    sig = _signal(SignalAction.ENTER_SHORT)
    closes = [100.0 + i * 0.2 for i in range(15)] + [103.0 - i * 0.5 for i in range(15)]
    result = assess_entry_context(sig, _bars(closes, ema9=98.0, ema20=100.0), Regime.TREND_UP)
    assert result.allowed is True


def test_scenario_11_sector_rotation():
    sig = _signal(SignalAction.ENTER_LONG, rs_score=0.95)
    closes = [100.0 + i * 0.1 for i in range(30)]
    result = assess_entry_context(sig, _bars(closes, ema9=102.0, ema20=100.0), Regime.TREND_UP)
    assert result.allowed is True


def test_scenario_12_market_stress():
    sig = _signal(SignalAction.ENTER_LONG, strategy="vwap_reversion")
    closes = [100.0 - i * 1.0 for i in range(30)]
    result = assess_entry_context(sig, _bars(closes, ema9=70.0, ema20=80.0), Regime.VOLATILE)
    assert result.allowed is True


def test_scenario_13_end_of_day():
    sig = _signal(SignalAction.ENTER_LONG)
    closes = [100.0 + i * 0.1 for i in range(30)]
    result = assess_entry_context(sig, _bars(closes, ema9=102.0, ema20=100.0), Regime.TREND_UP)
    assert result.allowed is True
