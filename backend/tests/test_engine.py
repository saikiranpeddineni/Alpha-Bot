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

    assert provider_calls == 1
    assert published_positions, "the tick must broadcast the already-fetched positions snapshot"

    await engine.stop()


async def test_engine_rejects_live_without_gates(engine: TradingEngine):
    with pytest.raises(PermissionError):
        await engine.start("live", confirm="LIVE")
    assert engine.state == EngineState.STOPPED
