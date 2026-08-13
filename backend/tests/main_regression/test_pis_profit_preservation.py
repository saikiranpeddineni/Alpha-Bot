"""PIS profit-preservation invariants for market-condition regression."""
from __future__ import annotations

from datetime import datetime, timezone

from app.pis.models import PISConfig, PositionObservation, TradeHealth
from app.pis.protection import AdaptiveProtectionPlanner


def observation(*, price: float, r: float, side: float = 1.0, entry: float = 100.0, stop: float = 99.0, atr: float = 0.5):
    return PositionObservation(
        timestamp=datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc),
        symbol="TEST",
        side=side,
        qty=100,
        entry_price=entry,
        current_price=price,
        high=max(price, entry),
        low=min(price, stop),
        close=price,
        volume=100_000,
        atr=atr,
        initial_stop=stop,
        risk_per_share=abs(entry - stop),
        r_multiple=r,
    )


def health(overall: float, volume: float = .8):
    return TradeHealth(overall, overall, overall, .5, volume, 0.0)


def test_profitable_trade_with_structural_deterioration_gets_intermediate_protection():
    planner = AdaptiveProtectionPlanner(PISConfig())
    floor = planner.compute_protection_floor(
        observation(price=100.6, r=.60),
        peak_r=.60,
        health=health(.30),
    )
    assert floor >= 100.05


def test_healthy_trade_can_keep_breathing_room_before_runner_activation():
    planner = AdaptiveProtectionPlanner(PISConfig())
    floor = planner.compute_protection_floor(
        observation(price=101.0, r=1.0, atr=.75),
        peak_r=1.0,
        health=health(.90),
    )
    assert floor == 100.3  # Profit protection ladder locks 0.3R at 1.0R peak


def test_scaled_out_winner_cannot_drop_below_locked_profit_floor():
    planner = AdaptiveProtectionPlanner(PISConfig())
    floor = planner.compute_protection_floor(
        observation(price=102.0, r=2.0),
        peak_r=2.0,
        health=health(.85),
        has_scaled_out=True,
    )
    assert floor >= 100.05


def test_monotonic_long_protection_never_weakens():
    planner = AdaptiveProtectionPlanner(PISConfig())
    previous = 100.40
    floor = planner.compute_protection_floor(
        observation(price=101.2, r=1.2),
        peak_r=1.2,
        health=health(.45),
        previous_floor=previous,
    )
    assert floor >= previous


def test_monotonic_short_protection_never_weakens():
    planner = AdaptiveProtectionPlanner(PISConfig())
    obs = observation(price=98.8, r=1.2, side=-1.0, stop=101.0)
    floor = planner.compute_protection_floor(
        obs,
        peak_r=1.2,
        health=health(.45),
        previous_floor=99.60,
    )
    assert floor <= 99.60
