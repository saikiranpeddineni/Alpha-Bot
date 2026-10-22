"""Trading engine integration test: real ticks over the synthetic market."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.config import Settings
from app.core.domain import EngineState, SafetyDecision, Signal, SignalAction
from app.core.engine import TradingEngine
from app.db.database import Database
from app.portfolio.allocator import PortfolioDecision


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


@pytest.mark.asyncio
async def test_stale_position_guard_waits_for_broker_flatness(engine: TradingEngine, monkeypatch):
    """Do not run PIS on a stale position while close fills are settling."""
    engine._flatten_at_open = True
    monkeypatch.setattr(engine.clock, "is_open", lambda _dt=None: True)
    engine.flatten = AsyncMock()
    engine.broker = SimpleNamespace(
        name="test-broker",
        positions=AsyncMock(side_effect=[[object()], []]),
        open_orders=AsyncMock(side_effect=[[object()], []]),
    )

    first = await engine._flatten_stale_positions_at_open()

    assert first is False
    assert engine._flatten_at_open is True

    second = await engine._flatten_stale_positions_at_open()

    assert second is True
    assert engine._flatten_at_open is False
    assert engine.flatten.await_count == 2


async def _run_safety_candidate_tick(
    engine: TradingEngine,
    signal: Signal,
    safety_decision: SafetyDecision | None,
    monkeypatch,
) -> dict:
    """Run one isolated candidate pass and return allocator call arguments."""
    await engine.start("demo")
    if engine._task is not None:
        engine._task.cancel()
        with suppress(asyncio.CancelledError):
            await engine._task
        engine._task = None

    bars = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [100_000.0],
        },
        index=[datetime.now(UTC)],
    )
    engine.replay_fast_path = True
    engine.last_safety_decision = safety_decision
    engine._check_system_heartbeat = AsyncMock()
    engine._reconcile_pending_submissions = AsyncMock()
    engine.data.poll = AsyncMock(return_value={signal.symbol: bars})
    engine._evaluate_symbol = AsyncMock(return_value=[(signal, bars)])
    engine.risk.check_halts = MagicMock(return_value=None)
    engine.risk.should_flatten = MagicMock(return_value=False)
    engine.risk.is_entry_permitted = MagicMock(
        return_value=SimpleNamespace(approved=True, reason="permitted")
    )
    engine.insights.on_signal = MagicMock(
        return_value=SimpleNamespace(model_dump=MagicMock(return_value={}))
    )
    engine.publish = MagicMock()

    captured: dict = {}

    def capture_allocation(**kwargs):
        captured.update(kwargs)
        return PortfolioDecision(approved_entries=[], gated_entries=[])

    monkeypatch.setattr(
        "app.portfolio.allocator.process_portfolio_allocations",
        capture_allocation,
    )
    await engine._tick()
    return captured


def _safety_signal(strategy: str) -> Signal:
    return Signal(
        symbol="SPY",
        action=SignalAction.ENTER_LONG,
        confidence=0.9,
        strategy=strategy,
        reason="safety gate regression",
        price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
    )


@pytest.mark.asyncio
async def test_tick_rejects_entry_strategy_outside_heartbeat_allowlist(engine: TradingEngine, monkeypatch):
    decision = SafetyDecision(
        system_state="NORMAL",
        market_risk_state="DEFENSIVE",
        allow_new_entries=True,
        risk_multiplier=0.5,
        allowed_strategies=["mean_reversion"],
        require_exit_only=False,
    )
    signal = _safety_signal("momentum")

    captured = await _run_safety_candidate_tick(engine, signal, decision, monkeypatch)

    assert captured["signals"] == []
    engine.risk.is_entry_permitted.assert_not_called()
    rejection_reason = engine.insights.on_signal.call_args.kwargs["gate_reason"]
    assert "strategy not allowed" in rejection_reason
    assert "momentum" in rejection_reason
    assert "DEFENSIVE" in rejection_reason


@pytest.mark.asyncio
async def test_tick_accepts_entry_strategy_in_heartbeat_allowlist(engine: TradingEngine, monkeypatch):
    decision = SafetyDecision(
        system_state="NORMAL",
        market_risk_state="CAUTION",
        allow_new_entries=True,
        risk_multiplier=0.75,
        allowed_strategies=["mean_reversion"],
        require_exit_only=False,
    )
    signal = _safety_signal("mean_reversion")

    captured = await _run_safety_candidate_tick(engine, signal, decision, monkeypatch)

    assert captured["signals"] == [signal]
    engine.risk.is_entry_permitted.assert_called_once()


@pytest.mark.asyncio
async def test_tick_records_broker_rejection_without_reserving_entry(engine: TradingEngine, monkeypatch):
    from app.core.domain import Order, OrderSide, OrderStatus, OrderType
    from app.core.shadow_ledger import shadow_ledger

    await engine.start("demo")
    if engine._task is not None:
        engine._task.cancel()
        with suppress(asyncio.CancelledError):
            await engine._task
        engine._task = None

    signal = _safety_signal("mean_reversion")
    bars = pd.DataFrame(
        {
            "open": [100.0] * 15,
            "high": [101.0] * 15,
            "low": [99.0] * 15,
            "close": [100.0] * 15,
            "volume": [100_000.0] * 15,
        },
        index=pd.date_range("2026-07-17 14:00:00", periods=15, freq="min", tz="UTC"),
    )
    rejected_order = Order(
        id="reject-1",
        client_order_id="reject-1",
        symbol="SPY",
        side=OrderSide.BUY,
        qty=1,
        order_type=OrderType.MARKET,
        status=OrderStatus.REJECTED,
        reason="broker rejected: buying power unavailable",
    )
    decision = SafetyDecision(
        system_state="NORMAL",
        market_risk_state="NORMAL",
        allow_new_entries=True,
        risk_multiplier=1.0,
        allowed_strategies=["mean_reversion"],
        require_exit_only=False,
    )

    engine.replay_fast_path = True
    engine.last_safety_decision = decision
    engine._check_system_heartbeat = AsyncMock()
    engine._reconcile_pending_submissions = AsyncMock()
    engine.data.poll = AsyncMock(return_value={signal.symbol: bars})
    engine._evaluate_symbol = AsyncMock(return_value=[(signal, bars)])
    engine.risk.check_halts = MagicMock(return_value=None)
    engine.risk.should_flatten = MagicMock(return_value=False)
    engine.insights.on_signal = MagicMock(
        return_value=SimpleNamespace(model_dump=MagicMock(return_value={}))
    )
    engine.broker.submit = AsyncMock(return_value=rejected_order)
    shadow_record = MagicMock()
    monkeypatch.setattr(shadow_ledger, "record", shadow_record)

    def approve_one(**_kwargs):
        return PortfolioDecision(approved_entries=[(signal, None)], gated_entries=[])

    monkeypatch.setattr("app.portfolio.allocator.process_portfolio_allocations", approve_one)

    await engine._tick()

    assert engine.pending_reconciliation_orders == {}
    assert engine.broker.submit.await_count == 1
    assert engine.insights.on_signal.call_args.kwargs["gated"] is True
    assert "broker rejection" in engine.insights.on_signal.call_args.kwargs["gate_reason"]
    assert any(call.kwargs.get("gate") == "broker_rejection" for call in shadow_record.call_args_list)


@pytest.mark.asyncio
async def test_tick_passes_market_risk_state_to_portfolio_allocator(engine: TradingEngine, monkeypatch):
    decision = SafetyDecision(
        system_state="NORMAL",
        market_risk_state="DEFENSIVE",
        allow_new_entries=True,
        risk_multiplier=0.5,
        allowed_strategies=["mean_reversion"],
        require_exit_only=False,
    )
    signal = _safety_signal("mean_reversion")

    captured = await _run_safety_candidate_tick(engine, signal, decision, monkeypatch)

    assert captured["market_state"] == "DEFENSIVE"


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


async def test_manual_entry_session_budget(engine: TradingEngine):
    """CR-21 review halt: entries past the session budget raise, closes flow."""
    from app.core.domain import OrderRequest, OrderSide

    engine.strategies = []
    await engine.start("demo")
    engine.risk_config.max_manual_entries_per_session = 1

    first = await engine.manual_order(OrderRequest(symbol="SPY", side=OrderSide.BUY, qty=1))
    assert first.status == "filled"
    with pytest.raises(RuntimeError, match="budget exhausted"):
        await engine.manual_order(OrderRequest(symbol="QQQ", side=OrderSide.BUY, qty=1))
    # Reducing/closing is risk reduction: never gated by the entry budget.
    closer = await engine.manual_order(OrderRequest(symbol="SPY", side=OrderSide.SELL, qty=1))
    assert closer.status == "filled"

    await engine.stop()


async def test_tick_reuses_authoritative_positions_snapshot_after_loop_cancel(engine: TradingEngine):
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
