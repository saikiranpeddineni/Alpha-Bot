"""Regression tests for issue #016 — startup broker reconciliation fail-closed gate.

Issue: Engine must not become entry-eligible when startup broker position
reconciliation fails or is uncertain. Entries must be blocked until
authoritative broker/local state is established. Exits remain available.

Tests follow the 5-phase state machine pattern from the dev guide:
  Phase 1 - Establish failure state
  Phase 2 - Prove safe operations (exits) still work
  Phase 3 - Prove dangerous operations (entries) are blocked
  Phase 4 - Trigger recovery (re-reconcile succeeds)
  Phase 5 - Prove recovery unblocks entries

Design: mirrors test_008_fail_closed_reconciliation.py — real TradingEngine
+ monkeypatch so all engine attributes are properly initialised via start().

NOTE: engine is started in "demo" mode (MarketClock always_open=True) so
that _tick()'s can_trade gate does not suppress signal evaluation based on
wall-clock time.  The reconciliation behaviour under test is identical across
all modes; demo is the correct mode for deterministic unit tests.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import Settings
from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position, Signal, SignalAction
from app.core.engine import TradingEngine, EngineState
from app.db.database import Database
from app.risk.manager import RiskDecision
from app.strategies.base import Strategy


# ---------------------------------------------------------------------------
# Shared fakes (identical to test_008)
# ---------------------------------------------------------------------------

class _FakeData:
    provider = SimpleNamespace(name="synthetic")

    async def warmup(self, _watchlist):
        return None

    async def poll(self, _watchlist):
        return {"AAPL": self.frame("AAPL", "1Min")}

    def frame(self, symbol, *args, **kwargs):
        idx = pd.date_range("2026-07-17 09:30:00", periods=60, freq="min", tz="UTC")
        sym_upper = symbol.upper().lstrip("^")
        val = 15.0 if sym_upper == "VIX" else 100.0
        return pd.DataFrame(
            {"open": [val]*60, "high": [val]*60, "low": [val]*60,
             "close": [val]*60, "volume": [1000.0]*60},
            index=idx,
        )

    async def history(self, symbol, *args, **kwargs):
        start = kwargs.get("start") or "2026-07-17 09:30:00"
        end = kwargs.get("end") or "2026-07-17 10:30:00"
        start_ts = pd.Timestamp(start)
        if start_ts.tz is None:
            start_ts = start_ts.tz_localize("UTC")
        else:
            start_ts = start_ts.tz_convert("UTC")
        end_ts = pd.Timestamp(end)
        if end_ts.tz is None:
            end_ts = end_ts.tz_localize("UTC")
        else:
            end_ts = end_ts.tz_convert("UTC")
        idx = pd.date_range(start_ts, end_ts, freq="min")
        sym_upper = symbol.upper().lstrip("^")
        val = 15.0 if sym_upper == "VIX" else 100.0
        return pd.DataFrame(
            {"open": [val]*len(idx), "high": [val]*len(idx), "low": [val]*len(idx),
             "close": [val]*len(idx), "volume": [1000.0]*len(idx)},
            index=idx,
        )


class _FakeBroker:
    name = "fake_broker"
    is_paper = True

    def __init__(self):
        self.should_fail_positions = False
        self.mock_positions: list[Position] = []
        self.submitted_orders: list[Order] = []
        self.closed_positions: list[tuple[str, str]] = []

    async def connect(self): pass

    async def account(self):
        return SimpleNamespace(equity=100_000.0, cash=100_000.0, buying_power=300_000.0)

    async def positions(self):
        if self.should_fail_positions:
            raise RuntimeError("Simulated positions fetch failure")
        return self.mock_positions

    async def open_orders(self):
        return []

    async def get_order_by_client_id(self, client_id: str):
        return None

    async def flatten_all(self, reason: str = ""):
        pass

    def drain_fills(self):
        return []

    async def submit(self, req: OrderRequest) -> Order:
        order = Order(
            id=req.client_order_id,
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            order_type=req.order_type,
            status=OrderStatus.SUBMITTED,
        )
        self.submitted_orders.append(order)
        return order

    async def close_position(self, symbol: str, reason: str = "manual") -> Order | None:
        self.closed_positions.append((symbol, reason))
        return Order(
            id=f"close-{symbol}",
            symbol=symbol,
            side=OrderSide.SELL,
            qty=10,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
        )


class _FakeStrategy(Strategy):
    name = "test_strat"
    enabled = True

    def __init__(self):
        super().__init__({})
        self.signals_to_return: list[Signal] = []

    def on_bar(self, ctx, symbol, df):
        return self.signals_to_return.pop(0) if self.signals_to_return else None


# ---------------------------------------------------------------------------
# Patch helpers (identical to test_008)
# ---------------------------------------------------------------------------

def _patch_pipeline_accept_all(pipeline):
    def permissive_process(symbol, df, signals, regime, features=None, sector_strength=None):
        accepted, rejected = [], []
        for sig in signals:
            sig.ml_probability = sig.ml_probability or 0.99
            sig.score = sig.score if sig.score is not None else 0.9
            accepted.append(sig)
        return accepted, rejected
    pipeline.process = permissive_process


def _patch_portfolio_gate_accept_all(engine):
    engine.portfolio_gate.gate = lambda symbol, positions, sector_override=None: (True, "ok")


def _patch_risk_accept_all(engine):
    import uuid

    def fake_evaluate_entry(sig, ctx, bars, now=None, kelly_override=None):
        req = OrderRequest(
            client_order_id=uuid.uuid4().hex,
            symbol=sig.symbol.upper(),
            side=OrderSide.BUY if sig.action == SignalAction.ENTER_LONG else OrderSide.SELL_SHORT,
            qty=1,
            order_type=OrderType.MARKET,
        )
        return RiskDecision(approved=True, reason="", order=req)

    engine.risk.evaluate_entry = fake_evaluate_entry


# ---------------------------------------------------------------------------
# Fixture: fully started engine (demo mode = always-open clock) with
# reconciliation pre-failed via a broker.positions() raise at startup.
# ---------------------------------------------------------------------------

@pytest.fixture
async def engine_with_failed_reconciliation(tmp_path, monkeypatch):
    """Provides a fully started TradingEngine whose startup reconciliation failed.

    Started in demo mode so MarketClock.is_open() is always True and
    _tick()'s can_trade gate never suppresses signal evaluation based on
    wall-clock time.
    """
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "bot.db"),
        DEFAULT_WATCHLIST="AAPL",
        DEMO_BAR_SECONDS=3600,
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)

    async def fake_ensure_data(self, mode=None):
        self.data = _FakeData()
        return self.data

    async def fake_run_loop(self):
        pass

    monkeypatch.setattr(TradingEngine, "ensure_data", fake_ensure_data)
    monkeypatch.setattr(TradingEngine, "_run_loop", fake_run_loop)

    broker = _FakeBroker()
    broker.should_fail_positions = True  # trigger startup reconciliation failure
    monkeypatch.setattr("app.core.engine.make_broker", lambda name, settings: broker)

    await engine.start("demo")

    _patch_pipeline_accept_all(engine.pipeline)
    _patch_portfolio_gate_accept_all(engine)
    _patch_risk_accept_all(engine)

    strat = _FakeStrategy()
    engine.strategies = [strat]

    return engine, broker, strat


# ---------------------------------------------------------------------------
# Tests — 5-phase state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1_startup_reconciliation_failure_sets_flag(
    engine_with_failed_reconciliation,
):
    """Phase 1: broker.positions() failure during start() sets reconciliation_failed=True."""
    engine, broker, strat = engine_with_failed_reconciliation
    assert engine.reconciliation_failed is True, (
        "reconciliation_failed must be True after broker.positions() raises at startup"
    )


@pytest.mark.asyncio
async def test_phase1_status_reflects_flag(engine_with_failed_reconciliation):
    """engine.status() must expose reconciliation_failed=True immediately after startup failure."""
    engine, broker, strat = engine_with_failed_reconciliation
    assert engine.status()["reconciliation_failed"] is True


@pytest.mark.asyncio
async def test_phase2_exits_still_execute_while_entry_gate_closed(
    engine_with_failed_reconciliation,
):
    """Phase 2: EXIT signals close positions even when reconciliation_failed=True."""
    engine, broker, strat = engine_with_failed_reconciliation

    exit_sig = Signal(
        symbol="AAPL", action=SignalAction.EXIT, confidence=1.0,
        strategy="test_strat", reason="take_profit_hit",
    )
    strat.signals_to_return.append(exit_sig)

    broker.mock_positions = [Position(symbol="AAPL", qty=10, avg_entry_price=100.0, market_price=100.0)]
    broker.should_fail_positions = False

    # Pin the flag to True: inhibit the real reconciler from clearing it this tick
    real_reconcile = engine._reconcile_positions
    engine._reconcile_positions = lambda positions: None

    await engine._tick()

    engine._reconcile_positions = real_reconcile  # restore

    assert len(broker.closed_positions) == 1
    assert broker.closed_positions[0] == ("AAPL", "take_profit_hit"), (
        "EXIT must still work while reconciliation_failed=True"
    )
    assert len(broker.submitted_orders) == 0, (
        "No entry order must be submitted while reconciliation_failed=True"
    )


@pytest.mark.asyncio
async def test_phase3_entries_blocked_while_reconciliation_failed(
    engine_with_failed_reconciliation,
):
    """Phase 3: ENTRY signals must be suppressed while reconciliation_failed=True."""
    engine, broker, strat = engine_with_failed_reconciliation

    entry_sig = Signal(
        symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=1.0,
        strategy="test_strat", reason="test_entry",
        price=100.0, stop_loss=90.0, take_profit=120.0, ml_probability=0.9,
    )
    strat.signals_to_return.append(entry_sig)
    broker.mock_positions = []
    broker.should_fail_positions = False

    # Pin the flag to True so entries stay blocked this tick
    real_reconcile = engine._reconcile_positions
    engine._reconcile_positions = lambda positions: None

    await engine._tick()

    engine._reconcile_positions = real_reconcile

    assert len(broker.submitted_orders) == 0, (
        "Entry order must NOT be submitted while reconciliation_failed=True"
    )


@pytest.mark.asyncio
async def test_phase4_successful_tick_clears_flag(engine_with_failed_reconciliation):
    """Phase 4: When broker.positions() succeeds on next tick, reconciliation_failed is cleared."""
    engine, broker, strat = engine_with_failed_reconciliation

    broker.should_fail_positions = False
    broker.mock_positions = []  # clean broker state

    await engine._tick()

    assert engine.reconciliation_failed is False, (
        "reconciliation_failed must be cleared after successful re-reconciliation in tick"
    )
    assert engine.status()["reconciliation_failed"] is False


@pytest.mark.asyncio
async def test_phase5_entries_unblocked_after_recovery(engine_with_failed_reconciliation):
    """Phase 5: After reconciliation_failed is cleared, entries must be submitted normally."""
    engine, broker, strat = engine_with_failed_reconciliation

    # Phase 4 — recover
    broker.should_fail_positions = False
    broker.mock_positions = []
    await engine._tick()
    assert engine.reconciliation_failed is False

    # Phase 5 — entry must now go through
    entry_sig = Signal(
        symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=1.0,
        strategy="test_strat", reason="test_entry",
        price=100.0, stop_loss=90.0, take_profit=120.0, ml_probability=0.9,
    )
    strat.signals_to_return.append(entry_sig)
    broker.submitted_orders.clear()
    engine.portfolio._lots.clear()

    await engine._tick()

    assert len(broker.submitted_orders) == 1, (
        f"Expected 1 submitted order after reconciliation cleared, "
        f"got {len(broker.submitted_orders)}. reconciliation_failed={engine.reconciliation_failed}"
    )
    assert broker.submitted_orders[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_reconcile_positions_long_and_short_clears_flag(
    engine_with_failed_reconciliation,
):
    """_reconcile_positions() with both long and short positions clears the flag."""
    engine, broker, strat = engine_with_failed_reconciliation

    positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=150.0, market_price=150.0),
        Position(symbol="TSLA", qty=-5, avg_entry_price=200.0, market_price=200.0),
    ]

    engine._reconcile_positions(positions)

    assert engine.reconciliation_failed is False, (
        "_reconcile_positions() must set reconciliation_failed=False on success"
    )
    assert "AAPL" in engine.portfolio._lots
    assert "TSLA" in engine.portfolio._lots
    assert engine.portfolio._lots["AAPL"].qty == 10
    assert engine.portfolio._lots["TSLA"].qty == -5


@pytest.mark.asyncio
async def test_engine_state_remains_running_after_reconciliation_failure(
    engine_with_failed_reconciliation,
):
    """Engine state must stay RUNNING (not HALTED) after reconciliation failure.

    Fail-closed only blocks entries; exits must remain available.
    """
    engine, broker, strat = engine_with_failed_reconciliation
    assert engine.state == EngineState.RUNNING, (
        "Engine must stay RUNNING after reconciliation failure — exits must still be available"
    )
    assert engine.reconciliation_failed is True
