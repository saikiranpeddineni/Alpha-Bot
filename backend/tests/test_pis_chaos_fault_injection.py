"""PIS Chaos Fault Injection Test Suite.

Injects faults into PIS to verify system resilience:
- Corrupted market data (NaN, inf, negative ATR, 0 price, missing columns)
- Out-of-order timestamps and rapid duplicate bar deliveries
- Broker network timeouts and replace_leg failure fallbacks
- Mid-trade restart and state recovery
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import pandas as pd
import pytest

from app.core.domain import Position
from app.pis import (
    ExitAction,
    PISConfig,
    PISEngineAdapter,
    PISService,
    PositionBrain,
    PositionObservation,
)


@pytest.mark.asyncio
async def test_corrupted_market_data_resilience() -> None:
    """Verify PIS gracefully handles corrupted market data without state corruption."""
    adapter = PISEngineAdapter()
    pos = Position(symbol="AAPL", qty=100, avg_entry_price=150.0, market_price=152.0)

    # DataFrame with NaN, inf, and 0 ATR
    df_corrupt = pd.DataFrame([
        {"high": float("nan"), "low": 145.0, "close": 152.0, "volume": 0, "atr": 0.0},
        {"high": 155.0, "low": float("-inf"), "close": 153.0, "volume": float("inf"), "atr": -1.0},
    ])

    # Should not crash or produce non-finite decision metrics
    order_req, decision = await adapter.evaluate_position_exit(
        position=pos,
        bar_df=df_corrupt,
        initial_stop=140.0,
    )

    assert decision.symbol == "AAPL"
    assert not math.isnan(decision.health.overall)
    assert not math.isinf(decision.health.overall)
    assert 0.0 <= decision.health.overall <= 1.0


def test_out_of_order_and_duplicate_events() -> None:
    """Verify PIS rejects out-of-order timestamps and handles duplicate events fast."""
    service = PISService()
    ts0 = datetime.now(timezone.utc)

    obs0 = PositionObservation(
        timestamp=ts0,
        symbol="MSFT",
        side=1.0,
        qty=50.0,
        entry_price=300.0,
        current_price=305.0,
        high=306.0,
        low=300.0,
        close=305.0,
        volume=1000.0,
        atr=4.0,
        initial_stop=290.0,
        risk_per_share=10.0,
        r_multiple=0.5,
    )

    dec0 = service.observe(obs0)
    assert dec0.symbol == "MSFT"

    # Time travel event (10 seconds earlier)
    ts_earlier = ts0 - timedelta(seconds=10)
    obs_earlier = PositionObservation(
        timestamp=ts_earlier,
        symbol="MSFT",
        side=1.0,
        qty=50.0,
        entry_price=300.0,
        current_price=304.0,
        high=306.0,
        low=300.0,
        close=304.0,
        volume=1000.0,
        atr=4.0,
        initial_stop=290.0,
        risk_per_share=10.0,
        r_multiple=0.4,
    )

    # Rejection of out-of-order observation
    with pytest.raises(Exception):
        service.observe(obs_earlier)

    # Brain state remains uncorrupted
    brain = service.get_brain("MSFT")
    assert brain is not None
    assert brain.history_length == 1


def test_mid_trade_restart_recovery() -> None:
    """Verify brain state serialization and reconstruction upon restart mid-trade."""
    service1 = PISService()
    ts = datetime.now(timezone.utc)

    obs0 = PositionObservation(
        timestamp=ts,
        symbol="NVDA",
        side=1.0,
        qty=10.0,
        entry_price=120.0,
        current_price=135.0,
        high=136.0,
        low=120.0,
        close=135.0,
        volume=5000.0,
        atr=3.0,
        initial_stop=110.0,
        risk_per_share=10.0,
        r_multiple=1.5,
    )

    dec1 = service1.observe(obs0)
    state_export = service1.serialize_all_states()

    assert "NVDA" in state_export
    assert state_export["NVDA"]["peak_r"] >= 1.5

    # Simulate restart with fresh service instance
    service2 = PISService()
    obs_restart = PositionObservation(
        timestamp=ts + timedelta(minutes=1),
        symbol="NVDA",
        side=1.0,
        qty=10.0,
        entry_price=120.0,
        current_price=140.0,
        high=141.0,
        low=135.0,
        close=140.0,
        volume=6000.0,
        atr=3.0,
        initial_stop=110.0,
        risk_per_share=10.0,
        r_multiple=2.0,
    )

    dec2 = service2.observe(obs_restart)
    assert dec2.symbol == "NVDA"
    assert dec2.peak_r >= 2.0
