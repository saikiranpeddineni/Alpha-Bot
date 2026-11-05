import pytest
import pandas as pd
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.core.domain import OrderRequest, OrderSide, OrderStatus, OrderType, Position, Regime, Signal, SignalAction
from app.strategies.base import StrategyContext
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.gap_and_go import GapAndGo
from app.indicators.ta import enrich


def test_orb_vwap_fade_volatility_buffer():
    """Verify that ORB does not exit on minor 1m noise around VWAP and requires buffer + 3 bars."""
    strat = OpeningRangeBreakout()
    strat.params["vwap_fade_bars"] = 3
    symbol = "ISRG"

    # Simulated position: entered at 477.46, stop at 471.63
    mock_pos = SimpleNamespace(qty=29, avg_entry_price=477.46, strategy="orb")
    # =============================================================================
    # [MODIFIED 2026-11-06]: Deterministic Morning Time for Test Isolation
    # ORIGINAL INTENT: Used datetime.now(UTC) for ctx.now and test bar timestamps.
    # REASON FOR CHANGE: If tests run after 15:55 ET, ORB triggers EOD exit instead of testing
    #   the VWAP fade buffer. Using a fixed 10:00 AM ET timestamp ensures deterministic isolation.
    # SAFE ROLLBACK: Previous lines preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # ctx = StrategyContext(position=mock_pos, equity=100000.0, regime=Regime.TREND_UP, now=datetime.now(UTC))
    # idx = [datetime.now(UTC) + timedelta(minutes=i) for i in range(20)]
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    test_now = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    ctx = StrategyContext(position=mock_pos, equity=100000.0, regime=Regime.TREND_UP, now=test_now)
    idx = [test_now + timedelta(minutes=i) for i in range(20)]
    # =============================================================================
    # Close is 473.80, VWAP is 474.00, ATR is 2.0 (so buffer = 0.15 * 2.0 = 0.30 -> threshold is 473.70)
    # 473.80 is > 473.70, so within buffer -> should NOT count as fade bar
    df = pd.DataFrame({
        "open": [474.0]*20,
        "high": [475.0]*20,
        "low": [473.0]*20,
        "close": [473.80]*20,
        "volume": [5000.0]*20,
        "vwap": [474.00]*20,
        "atr14": [2.0]*20,
    }, index=idx)

    # Calling on_bar with close within buffer should NOT produce an exit signal
    sig = strat.on_bar(ctx, symbol, df)
    assert sig is None
    assert strat._vwap_fade_count.get(symbol, 0) == 0

    # Now let close break decisively below buffer: close 473.00 < (474.00 - 0.30 = 473.70)
    df_breach = pd.DataFrame({
        "open": [474.0]*20,
        "high": [475.0]*20,
        "low": [472.5]*20,
        "close": [473.00]*20,
        "volume": [5000.0]*20,
        "vwap": [474.00]*20,
        "atr14": [2.0]*20,
    }, index=idx)

    # Bar 1: fade_count = 1 -> no exit
    sig1 = strat.on_bar(ctx, symbol, df_breach)
    assert sig1 is None
    assert strat._vwap_fade_count.get(symbol, 0) == 1

    # Bar 2: fade_count = 2 -> no exit (requires 3 bars)
    sig2 = strat.on_bar(ctx, symbol, df_breach)
    assert sig2 is None
    assert strat._vwap_fade_count.get(symbol, 0) == 2

    # Bar 3: fade_count = 3 -> exit fired
    sig3 = strat.on_bar(ctx, symbol, df_breach)
    assert sig3 is not None
    assert sig3.action == SignalAction.EXIT
    assert "Momentum faded (Close < VWAP)" in sig3.reason


def test_gap_and_go_stop_loss_scaled_by_atr():
    """Verify that Gap & Go stops incorporate ATR buffer rather than hardcoded 5 cents."""
    strat = GapAndGo()
    symbol = "SYK"
    ctx = StrategyContext(position=None, equity=100000.0, regime=Regime.TREND_UP, now=datetime(2026, 11, 4, 14, 52, tzinfo=UTC), mover_change_pct=4.0)

    # Create 30 bars of gap up data where close > vwap, ema9 > ema20, close > ema20
    idx = [datetime(2026, 11, 4, 14, 30, tzinfo=UTC) + timedelta(minutes=i) for i in range(25)]
    # Close 331.48, EMA20 will be around 328.35, ATR14 around 1.04
    df = pd.DataFrame({
        "open": [326.0] + [330.0]*24,
        "high": [328.0] + [332.0]*24,
        "low": [325.0] + [329.0]*24,
        "close": [327.0] + [331.48]*24,
        "volume": [100000.0]*25,
    }, index=idx)
    df = enrich(df)

    sig = strat.on_bar(ctx, symbol, df)
    assert sig is not None
    assert sig.action == SignalAction.ENTER_LONG
    atr_val = float(df["atr14"].iloc[-1])
    ema20_val = float(df["ema20"].iloc[-1])
    expected_buffer = max(0.05, 0.25 * atr_val)

    # Stop should be ema20 - expected_buffer, NOT ema20 - 0.05
    assert sig.stop_loss == pytest.approx(ema20_val - expected_buffer, abs=1e-3)
    assert sig.stop_loss < (ema20_val - 0.05)


@pytest.mark.asyncio
async def test_alpaca_nested_child_fills_unrolled(monkeypatch):
    """Verify AlpacaBroker poll_fills unpacks nested legs of bracket orders."""
    from app.brokers.alpaca import AlpacaBroker
    from app.core.domain import OrderSide

    broker = AlpacaBroker("fake_key", "fake_sec", "https://paper-api.alpaca.markets")
    broker.account_id = "test_acc"

    # Parent order with child stop leg
    parent_id = "parent-order-1"
    child_stop_id = "child-stop-1"

    child_order = SimpleNamespace(
        id=child_stop_id,
        symbol="MRNA",
        side="sell",
        qty="44.0",
        filled_qty="44.0",
        filled_avg_price="241.88",
        filled_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        submitted_at=datetime.now(UTC),
        order_type="stop",
        type="stop",
        legs=[],
    )

    parent_order = SimpleNamespace(
        id=parent_id,
        symbol="MRNA",
        side="buy",
        qty="44.0",
        filled_qty="44.0",
        filled_avg_price="252.00",
        filled_at=datetime.now(UTC) - timedelta(hours=1),
        updated_at=datetime.now(UTC) - timedelta(hours=1),
        submitted_at=datetime.now(UTC) - timedelta(hours=1),
        order_type="market",
        type="market",
        legs=[child_order],
    )

    # Mark parent as already processed so only child fill is emitted
    broker._processed_fill_qty[parent_id] = 44.0
    broker._meta[parent_id] = ("gap_and_go", "entry")

    async def fake_paginate(*args, **kwargs):
        return [parent_order]

    import app.brokers.alpaca as legacy
    monkeypatch.setattr(legacy, "_paginate_orders", fake_paginate)

    await broker.poll_fills()
    fills = broker.drain_fills()

    # Child stop fill must be emitted
    assert len(fills) == 1
    f = fills[0]
    assert f.order_id == child_stop_id
    assert f.symbol == "MRNA"
    assert f.side == OrderSide.SELL
    assert f.qty == 44.0
    assert f.price == 241.88
    assert f.strategy == "gap_and_go"
    assert f.reason == "stop_loss"
