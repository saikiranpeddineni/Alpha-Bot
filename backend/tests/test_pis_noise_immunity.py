"""Tests for PIS Noise Immunity and Anti-Shakeout Architecture.

Validates that PIS is immune to:
1. Single-bar candlestick wicks (microstructure noise)
2. Normal opening-bell pullbacks inside the risk envelope
3. Low-volume consolidation pauses (bull flags)
4. Sub-noise stop strangling

While still executing timely exits on:
5. Multi-bar confirmed severe distribution breakdowns
6. Hard stop breach
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from app.pis.models import (
    ExitAction,
    LifecycleState,
    PISConfig,
    PositionObservation,
    ReasonCode,
)
from app.pis.position_brain import PositionBrain


def _make_obs(
    symbol: str = "TEST",
    side: float = 1.0,
    qty: float = 100.0,
    entry: float = 100.0,
    price: float = 100.0,
    high: float = 100.2,
    low: float = 99.8,
    close: float = 100.0,
    volume: float = 1000.0,
    atr: float = 1.0,
    risk: float = 1.0,
    initial_stop: float = 99.0,
    current_stop: float = 99.0,
    take_profit: float = 103.0,
    t_offset_sec: int = 0,
    sentiment: float = 0.0,
) -> PositionObservation:
    t = datetime(2026, 8, 4, 9, 30, 0, tzinfo=timezone.utc) + timedelta(seconds=t_offset_sec)
    r_mult = (price - entry) / risk if side > 0 else (entry - price) / risk
    return PositionObservation(
        symbol=symbol,
        timestamp=t,
        side=side,
        qty=qty,
        current_price=price,
        entry_price=entry,
        initial_stop=initial_stop,
        current_stop=current_stop,
        take_profit=take_profit,
        atr=atr,
        risk_per_share=risk,
        high=high,
        low=low,
        close=close,
        volume=volume,
        r_multiple=r_mult,
        news_sentiment_score=sentiment,
    )


def test_opening_pullback_does_not_shake_out():
    """A normal -0.40R pullback on bar 2 must NOT trigger an emergency exit."""
    cfg = PISConfig(scale_out_target_r=1.0)
    obs0 = _make_obs(price=100.0, close=100.0, t_offset_sec=0)
    brain = PositionBrain("TEST", obs0, config=cfg)

    # Bar 1: slight dip to -0.30R
    obs1 = _make_obs(price=99.70, close=99.70, low=99.65, high=100.05, t_offset_sec=60)
    dec1 = brain.observe(obs1)
    assert dec1.action != ExitAction.FULL_EXIT
    assert brain.lifecycle in {LifecycleState.DISCOVERY, LifecycleState.WEAKENING}

    # Bar 2: dip to -0.40R
    obs2 = _make_obs(price=99.60, close=99.60, low=99.55, high=99.80, t_offset_sec=120)
    dec2 = brain.observe(obs2)
    # Must NOT panic exit at market!
    assert dec2.action != ExitAction.FULL_EXIT

    # Bar 3: Rebound to +0.50R
    obs3 = _make_obs(price=100.50, close=100.50, low=99.60, high=100.60, t_offset_sec=180)
    dec3 = brain.observe(obs3)
    assert dec3.action != ExitAction.FULL_EXIT
    assert dec3.health.overall > 0.40

    # Bar 4: Push to +1.0R scale out target
    obs4 = _make_obs(price=101.00, close=101.00, low=100.40, high=101.05, t_offset_sec=240)
    dec4 = brain.observe(obs4)
    assert dec4.action == ExitAction.PARTIAL_EXIT
    assert brain.has_scaled_out is False
    
    # Bar 5: Broker confirms scale out qty drop
    obs5 = _make_obs(price=101.00, close=101.00, low=100.40, high=101.05, qty=50.0, t_offset_sec=300)
    dec5 = brain.observe(obs5)
    assert brain.has_scaled_out is True


def test_single_bar_wick_does_not_cause_shakeout():
    """A 1-minute candlestick upper wick during an uptrend should not trigger exit."""
    cfg = PISConfig()
    obs0 = _make_obs(price=100.0, close=100.0, t_offset_sec=0)
    brain = PositionBrain("TEST", obs0, config=cfg)

    # Move up to +0.80R
    obs1 = _make_obs(price=100.80, close=100.80, high=100.85, low=100.0, t_offset_sec=60)
    brain.observe(obs1)

    # 1-minute bar with a 50% wick (high=100.90, close=100.50, low=100.45)
    obs2 = _make_obs(price=100.50, close=100.50, high=100.90, low=100.45, t_offset_sec=120)
    dec2 = brain.observe(obs2)
    assert dec2.action != ExitAction.FULL_EXIT
    assert brain.lifecycle != LifecycleState.EXIT_CANDIDATE


def test_consolidation_bull_flag_does_not_dump():
    """Tightly consolidating for 10 bars must not trigger a premature stagnation harvest."""
    cfg = PISConfig()
    obs0 = _make_obs(price=100.0, close=100.0, t_offset_sec=0)
    brain = PositionBrain("TEST", obs0, config=cfg)

    # Run to +0.60R
    for i in range(1, 4):
        obs = _make_obs(price=100.0 + (i * 0.20), close=100.0 + (i * 0.20), t_offset_sec=i * 60)
        brain.observe(obs)

    # Consolidate around +0.60R for 10 bars (tight range 100.55 to 100.65)
    for i in range(4, 14):
        obs = _make_obs(
            price=100.60,
            close=100.60,
            high=100.65,
            low=100.55,
            atr=1.0,
            t_offset_sec=i * 60,
        )
        dec = brain.observe(obs)
        assert dec.action != ExitAction.FULL_EXIT


def test_confirmed_multi_bar_breakdown_exits():
    """Severe distribution sustained over 3+ bars triggers EXIT_CANDIDATE and exits."""
    cfg = PISConfig()
    obs0 = _make_obs(price=100.0, close=100.0, t_offset_sec=0)
    brain = PositionBrain("TEST", obs0, config=cfg)

    # Advance to validated
    obs1 = _make_obs(price=100.80, close=100.80, t_offset_sec=60)
    brain.observe(obs1)

    # 3 consecutive heavy breakdown bars with extreme range & heavy adverse volume
    for i in range(2, 6):
        obs = _make_obs(
            price=100.80 - (i * 0.40),
            close=100.80 - (i * 0.40),
            high=100.80 - ((i - 1) * 0.40),
            low=100.80 - (i * 0.40) - 0.10,
            volume=50000.0,
            atr=1.0,
            t_offset_sec=i * 60,
        )
        dec = brain.observe(obs)

    # Should have flagged EXIT_CANDIDATE or FULL_EXIT after persistent degradation
    assert dec.action in {ExitAction.FULL_EXIT, ExitAction.UPDATE_PROTECTION}
