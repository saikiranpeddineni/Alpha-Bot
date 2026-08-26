"""Unit tests for PIS 50/50 Scale-Out & Runner Trailing Stop Architecture."""

import pytest
from datetime import datetime, timezone, timedelta
from app.pis.models import (
    PISConfig,
    PositionObservation,
    ExitAction,
    ReasonCode,
    LifecycleState,
)
from app.pis.position_brain import PositionBrain
from app.pis.service import PISService
from app.pis.execution import PISExecutionAdapter


def make_obs(
    symbol: str = "TEST",
    price: float = 100.0,
    entry: float = 100.0,
    initial_stop: float = 95.0,
    atr: float = 2.0,
    qty: float = 100.0,
    side: float = 1.0,
    minutes_offset: int = 0,
) -> PositionObservation:
    risk = abs(entry - initial_stop)
    r_multiple = (price - entry) / risk * side
    ts = datetime(2026, 8, 4, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=minutes_offset)
    return PositionObservation(
        timestamp=ts,
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry,
        current_price=price,
        high=max(price, entry),
        low=min(price, entry),
        close=price,
        volume=10000.0,
        atr=atr,
        initial_stop=initial_stop,
        risk_per_share=risk,
        current_stop=initial_stop,
        take_profit=None,
        r_multiple=r_multiple,
        news_sentiment_score=0.0,
    )


def test_pis_scale_out_at_1r() -> None:
    """Test that at +1.0R, PIS triggers a 50% PARTIAL_EXIT and switches into runner mode."""
    config = PISConfig(
        enable_scale_out=True,
        scale_out_ratio=0.50,
        scale_out_target_r=1.0,
        runner_wide_atr_mult=2.0,
    )
    
    # 1. Entry at 100.0, Stop at 95.0 (Risk = 5.0).
    obs1 = make_obs(price=100.0, entry=100.0, initial_stop=95.0, atr=2.0, qty=100.0, minutes_offset=0)
    brain = PositionBrain("TEST", obs1, config)
    
    assert brain.has_scaled_out is False
    assert brain.last_decision.action == ExitAction.HOLD
    assert brain.current_stop_floor == 95.0

    # 2. Price climbs to 104.0 (+0.8R) -> still no scale-out. With atr=2.0 the
    #    noise unit is 0.4R, so the progressive-lock trigger sits at
    #    max(0.40, 2.5*0.4) = 1.0R: a sub-noise peak must not move the floor.
    obs2 = make_obs(price=104.0, entry=100.0, initial_stop=95.0, atr=2.0, qty=100.0, minutes_offset=1)
    dec2 = brain.observe(obs2)
    assert brain.has_scaled_out is False
    assert dec2.action == ExitAction.HOLD
    assert brain.current_stop_floor == 95.0

    # 3. Price reaches 105.0 (+1.0R) -> PARTIAL_EXIT 50%
    obs3 = make_obs(price=105.0, entry=100.0, initial_stop=95.0, atr=2.0, qty=100.0, minutes_offset=2)
    dec3 = brain.observe(obs3)
    assert dec3.action == ExitAction.PARTIAL_EXIT
    assert dec3.exit_ratio == 0.50
    assert ReasonCode.SCALE_OUT_TARGET in dec3.reason_codes
    assert brain.has_scaled_out is True

    # Check order request built by adapter
    order_req = PISExecutionAdapter.build_order_request(dec3, obs3)
    assert order_req is not None
    assert order_req.qty == 50.0

    # Check that stop floor moved to at least breakeven + wide ATR trailing stop
    # breakeven = 100.0 + (0.05 * 5.0) = 100.25
    # wide trail = 105.0 - (2.0 * 2.0) = 101.0
    assert brain.current_stop_floor >= 100.25

    # 4. Pullback to 103.0 (+0.6R) -> Does NOT exit early because runner uses wide trail!
    # Floor is 101.0, price is 103.0 > 101.0
    obs4 = make_obs(price=103.0, entry=100.0, initial_stop=95.0, atr=2.0, qty=50.0, minutes_offset=3)
    dec4 = brain.observe(obs4)
    assert dec4.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
    assert brain.has_scaled_out is True

    # 5. Massive trend continuation to 115.0 (+3.0R)
    # Stop floor trails up: 115.0 - (2.0 * 2.0) = 111.0
    obs5 = make_obs(price=115.0, entry=100.0, initial_stop=95.0, atr=2.0, qty=50.0, minutes_offset=4)
    dec5 = brain.observe(obs5)
    assert dec5.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
    assert brain.current_stop_floor >= 111.0

    # 6. Reversal breakdown below trailing stop to 110.0
    obs6 = make_obs(price=110.0, entry=100.0, initial_stop=95.0, atr=2.0, qty=50.0, minutes_offset=5)
    dec6 = brain.observe(obs6)
    assert dec6.action == ExitAction.FULL_EXIT
    assert ReasonCode.PROTECTION_BREACH in dec6.reason_codes


def test_pis_scale_out_fractional_shares() -> None:
    """Test scale-out with odd and fractional share sizes."""
    obs = make_obs(price=105.0, entry=100.0, initial_stop=95.0, atr=2.0, qty=3.0)
    config = PISConfig(enable_scale_out=True, scale_out_ratio=0.50, scale_out_target_r=1.0)
    brain = PositionBrain("TEST", obs, config)
    dec = brain.last_decision
    assert dec.action == ExitAction.PARTIAL_EXIT
    order_req = PISExecutionAdapter.build_order_request(dec, obs)
    assert order_req is not None
    # floor(3 * 0.5) = 1 share closed, leaving 2 shares runner
    assert order_req.qty == 1.0
