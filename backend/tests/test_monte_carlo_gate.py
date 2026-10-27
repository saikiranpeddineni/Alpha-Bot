"""Unit tests for Pre-Trade Monte Carlo Replay Gate (Time Machine)."""

import numpy as np
import pandas as pd
import pytest

from app.ai.monte_carlo_gate import MonteCarloGate
from app.core.domain import Signal, SignalAction


@pytest.fixture
def sample_bars_df():
    dates = pd.date_range("2026-10-23 09:30", periods=100, freq="1min")
    rng = np.random.default_rng(42)
    drift = np.linspace(0, 2.0, 100)
    noise = rng.normal(0, 0.05, 100)
    closes = 100.0 + drift + noise
    opens = closes - 0.01
    highs = np.maximum(opens, closes) + 0.08
    lows = np.minimum(opens, closes) - 0.06
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": 10000.0,
    }, index=dates)


def test_monte_carlo_gate_pool_extraction(sample_bars_df):
    gate = MonteCarloGate()
    pool = gate.extract_ratio_pool(sample_bars_df)
    assert pool is not None
    h_pool, l_pool, c_pool = pool
    assert len(h_pool) > 0
    assert len(l_pool) == len(h_pool)
    assert len(c_pool) == len(h_pool)
    assert np.all(h_pool >= l_pool)


def test_monte_carlo_gate_thin_frame_fails_closed():
    gate = MonteCarloGate()
    thin_df = pd.DataFrame({"close": [100.0, 101.0]})
    pool = gate.extract_ratio_pool(thin_df)
    assert pool is None

    sig = Signal(
        symbol="THIN",
        action=SignalAction.ENTER_LONG,
        confidence=0.8,
        reason="thin",
        price=101.0,
        stop_loss=100.0,
        take_profit=103.0,
        strategy="orb",
    )
    passed, stats, reason = gate.evaluate_signal(sig, thin_df)
    assert passed is False
    assert "failed closed" in reason


def test_monte_carlo_gate_vetoes_unfavorable_setup(sample_bars_df):
    gate = MonteCarloGate(min_win_prob=0.35, min_expected_r=0.05, n_replays=1000)

    # Setup with unrealistic target ($150) and razor-thin stop ($99.98)
    sig_bad = Signal(
        symbol="UNFAVORABLE",
        action=SignalAction.ENTER_LONG,
        confidence=0.8,
        reason="orb",
        price=100.0,
        stop_loss=99.98,
        take_profit=150.0,
        strategy="orb",
    )

    passed, stats, reason = gate.evaluate_signal(sig_bad, sample_bars_df)
    assert passed is False
    assert "Monte Carlo veto" in reason
    assert stats["p_target"] < 0.35


def test_monte_carlo_gate_approves_favorable_setup(sample_bars_df):
    gate = MonteCarloGate(min_win_prob=0.30, min_expected_r=0.0, n_replays=1000)

    last_close = float(sample_bars_df["close"].iloc[-1])
    sig_good = Signal(
        symbol="FAVORABLE",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        reason="orb",
        price=last_close,
        stop_loss=last_close - 0.30,
        take_profit=last_close + 0.30,
        strategy="orb",
    )

    passed, stats, reason = gate.evaluate_signal(sig_good, sample_bars_df)
    assert passed is True
    assert "Monte Carlo approved" in reason
    assert stats["p_target"] > 0.0


def test_monte_carlo_gate_in_trade_exit_trigger(sample_bars_df):
    gate = MonteCarloGate(n_replays=1000)

    # When price is stalled far from target ($140) with tight stop ($99.98) and only 10 bars left
    should_exit, stats, reason = gate.evaluate_in_trade_exit(
        current_price=100.0,
        stop_loss=99.98,
        take_profit=140.0,
        is_long=True,
        df=sample_bars_df,
        bars_left=10,
        min_continuation_prob=0.25,
    )

    assert should_exit is True
    assert "Time Machine early exit" in reason
    assert stats["p_target"] < 0.25
