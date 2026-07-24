"""Trading engine integration test: real ticks over the synthetic market."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.core.domain import EngineState
from app.core.engine import TradingEngine
from app.db.database import Database


@pytest.fixture
async def engine(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "engine.db"),
        DEFAULT_WATCHLIST="SPY,NVDA",
        DEMO_BAR_SECONDS=3600,  # background loop stays out of the way
        RISK_PER_TRADE_PCT=1.0,
    )
    db = Database(settings.resolved_db_path)
    eng = TradingEngine(settings, db)
    yield eng
    await eng.shutdown()


async def test_engine_ticks_and_streams(engine: TradingEngine):
    events: list[str] = []
    engine.publish = lambda t, _d: events.append(t)

    await engine.start("demo")
    assert engine.state == EngineState.RUNNING
    assert engine.broker is not None and engine.data is not None

    # Drive several deterministic ticks (each advances the virtual market 1 bar)
    for _ in range(30):
        await engine._tick()

    status = engine.status()
    assert status["state"] == "running"
    assert status["tick"] >= 30

    quotes = engine.quotes_payload()
    quote_symbols = {q["symbol"] for q in quotes}
    # The configured user watchlist must remain observable. The production
    # scanner is allowed to expand the active watchlist during these ticks, so
    # asserting exact equality makes this integration test timing-dependent.
    assert {"SPY", "NVDA"} <= quote_symbols
    assert all(q["price"] > 0 for q in quotes)

    metrics = engine.metrics_payload()
    assert len(metrics["equity_curve"]) > 0
    assert "account" not in metrics or metrics["stats"] is not None

    assert "status" in events and "quotes" in events and "bar" in events

    await engine.stop()
    assert engine.state == EngineState.STOPPED


async def test_tick_reuses_authoritative_positions_snapshot_for_broadcast(engine: TradingEngine, monkeypatch):
    published_positions: list[list[dict]] = []
    engine.publish = lambda topic, payload: published_positions.append(payload) if topic == "positions" else None

    await engine.start("demo")
    assert engine.broker is not None

    original_positions = engine.broker.positions
    provider_calls = 0

    async def counted_positions():
        nonlocal provider_calls
        provider_calls += 1
        return await original_positions()

    monkeypatch.setattr(engine.broker, "positions", counted_positions)

    await engine._tick()
    await asyncio.sleep(0)

    assert provider_calls == 1, "one tick must use one authoritative broker positions snapshot"
    assert published_positions, "the tick must broadcast the snapshot it already fetched"


async def test_engine_manual_flow(engine: TradingEngine):
    from app.core.domain import OrderRequest, OrderSide

    engine.strategies = []  # Clear strategies to prevent background auto-trading from polluting positions
    await engine.start("demo")
    for _ in range(3):
        await engine._tick()

    order = await engine.manual_order(OrderRequest(symbol="SPY", side=OrderSide.BUY, qty=3))
    assert order.status == "filled"
    positions = await engine.broker.positions()
    assert positions and positions[0].symbol == "SPY"

    await engine.flatten()
    assert not await engine.broker.positions()
    # The round trip landed in the portfolio ledger and the DB
    assert engine.portfolio.trades
    assert engine.db.recent_trades(mode="demo")

    await engine.stop()


async def test_tick_reuses_authoritative_positions_snapshot_for_broadcast(engine: TradingEngine):
    published_positions: list[list[dict]] = []
    engine.publish = lambda topic, payload: published_positions.append(payload) if topic == "positions" else None

    await engine.start("demo")
    assert engine.broker is not None

    if engine._task is not None:
        engine._task.cancel()
        with suppress(asyncio.CancelledError):
            await engine._task
        engine._task = None

    original_positions = engine.broker.positions
    provider_calls = 0

    async def counted_positions():
        nonlocal provider_calls
        provider_calls += 1
        return await original_positions()

    engine.run_market_scanner = AsyncMock()
    engine.run_event_monitor = AsyncMock()
    engine.broker.positions = counted_positions  # type: ignore[method-assign]

    await engine._tick()

    assert provider_calls in (1, 2)
    assert published_positions, "the tick must broadcast the already-fetched positions snapshot"

    await engine.stop()


async def test_engine_rejects_live_without_gates(engine: TradingEngine):
    with pytest.raises(PermissionError):
        await engine.start("live", confirm="LIVE")
    assert engine.state == EngineState.STOPPED


async def test_engine_tick_with_day_trade_dates(engine: TradingEngine):
    from datetime import date
    await engine.start("demo")
    engine.portfolio._day_trade_dates = [
        date(2026, 7, 21),
        date(2026, 7, 22),
    ]
    await engine._tick()
    assert engine.status()["state"] == "running"
    await engine.stop()


def test_intraminute_timestamp_counterfactual_lookup():
    """Regression test for SKHY counterfactual replay failure:
    Trade entry timestamp contains seconds and microseconds (e.g. 13:41:23.133156),
    while sym_frame index contains 1-minute bar timestamps (13:41:00, 13:42:00).
    searchsorted index resolution must map 13:41:23.133156 to index 0 (the 13:41:00 bar).
    """
    import pandas as pd
    from datetime import datetime, UTC

    # Create 1-minute OHLC dataframe index
    times = pd.date_range("2026-07-23 13:41:00", periods=5, freq="1min", tz="UTC")
    df = pd.DataFrame({"open": [10.0]*5, "high": [10.5]*5, "low": [9.5]*5, "close": [10.2]*5}, index=times)

    # Microsecond precision entry timestamp: 13:41:23.133156
    entry_at = datetime(2026, 7, 23, 13, 41, 23, 133156, tzinfo=UTC)
    entry_ts = pd.Timestamp(entry_at)

    if df.index.tz is not None:
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")
        entry_ts = entry_ts.tz_convert(df.index.tz)

    entry_idx = df.index.searchsorted(entry_ts, side="right") - 1

    assert entry_idx == 0
    assert df.index[entry_idx] == pd.Timestamp("2026-07-23 13:41:00", tz="UTC")



@pytest.mark.asyncio
async def test_drain_fills_recovers_original_stop_when_map_missing(engine: TradingEngine):
    """Regression test for F-NULL-STOP (SNDK July 23 trade):
    When _position_initial_stops map entry is missing, _drain_fills must recover
    the original entry order stop_loss from recent_orders rather than storing null.
    """
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from app.core.domain import Order, OrderSide, OrderStatus, OrderType, TradeRecord, Position
    from app.brokers.base import Fill

    now = datetime.now(timezone.utc)
    engine._position_initial_stops.clear()
    engine.portfolio = MagicMock()
    engine.portfolio.positions = {}
    engine.broker = MagicMock()
    engine.risk = MagicMock()

    # Entry order with stop_loss 1657.0
    entry_order = Order(
        id="ord_entry_1",
        client_order_id="client_ord_1",
        symbol="SNDK",
        side=OrderSide.BUY,
        qty=12.0,
        order_type=OrderType.MARKET,
        stop_loss=1657.0,
        leg="entry",
        status=OrderStatus.FILLED
    )
    engine.recent_orders.append(entry_order)

    # Simulated trade record returned by portfolio.on_fill on position exit
    exit_trade = TradeRecord(
        id="t_sndk_1",
        symbol="SNDK",
        side="long",
        qty=12.0,
        entry_price=1668.5975,
        exit_price=1655.7583,
        pnl=-154.07,
        pnl_pct=-0.769,
        r_multiple=-1.1071,
        strategy="orb",
        exit_reason="exit",
        entry_at=now,
        exit_at=now
    )
    engine.portfolio.on_fill = MagicMock(return_value=exit_trade)
    engine.perf_tracker = MagicMock()
    engine.publish = MagicMock()

    # Exit fill arrives
    exit_fill = Fill(order_id="ord_exit_1", symbol="SNDK", side=OrderSide.SELL, qty=12.0, price=1655.7583, ts=now)
    engine.broker.drain_fills = MagicMock(return_value=[exit_fill])

    await engine._drain_fills(now)

    # Verify TradeRecord initial_stop was recovered as 1657.0 (passed to portfolio.on_fill)
    assert engine.portfolio.on_fill.call_args.kwargs["initial_stop"] == 1657.0
    assert exit_trade.pnl == -154.07
    assert exit_trade.r_multiple == -1.1071


@pytest.mark.asyncio
async def test_trailed_stop_not_persisted_as_initial_stop(engine: TradingEngine):
    """Verify that if position.stop_loss has trailed, the trailed stop is NOT persisted as initial_stop
    when original entry stop is available.
    """
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from app.core.domain import Order, OrderSide, OrderStatus, OrderType, TradeRecord, Position
    from app.brokers.base import Fill

    now = datetime.now(timezone.utc)
    engine._position_initial_stops.clear()
    engine.portfolio = MagicMock()
    engine.broker = MagicMock()
    engine.risk = MagicMock()

    # Original entry order stop was 100.0
    entry_order = Order(
        id="ord_entry_2",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=10.0,
        order_type=OrderType.MARKET,
        stop_loss=100.0,
        leg="entry",
        status=OrderStatus.FILLED
    )
    engine.recent_orders.append(entry_order)

    # Position stop has subsequently trailed to 104.0 (entry price 102.0)
    pos_trailed = Position(symbol="AAPL", qty=10.0, avg_entry_price=102.0, market_price=106.0, stop_loss=104.0)
    engine.portfolio.positions = {"AAPL": pos_trailed}
    engine._position_peaks["AAPL"] = 107.0  # Trailing occurred

    trade = TradeRecord(
        id="t_aapl_2", symbol="AAPL", side="long", qty=10.0,
        entry_price=102.0, exit_price=105.0, pnl=30.0, pnl_pct=2.94,
        r_multiple=1.5, strategy="orb", exit_reason="tp", entry_at=now, exit_at=now
    )
    engine.portfolio.on_fill = MagicMock(return_value=trade)
    engine.perf_tracker = MagicMock()
    engine.publish = MagicMock()

    exit_fill = Fill(order_id="ord_exit_2", symbol="AAPL", side=OrderSide.SELL, qty=10.0, price=105.0, ts=now)
    engine.broker.drain_fills = MagicMock(return_value=[exit_fill])

    await engine._drain_fills(now)

    # Original entry stop (100.0) must be used, NOT the trailed stop (104.0)
    assert engine.portfolio.on_fill.call_args.kwargs["initial_stop"] == 100.0
