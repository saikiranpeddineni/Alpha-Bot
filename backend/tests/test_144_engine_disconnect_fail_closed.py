from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pandas as pd

from app.config import Settings
from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position, Signal, SignalAction
from app.core.engine import TradingEngine, EngineState
from app.db.database import Database
from app.risk.manager import RiskDecision
from app.strategies.base import Strategy


class _FakeData:
    provider = SimpleNamespace(name="synthetic")

    async def warmup(self, _watchlist):
        return None

    async def poll(self, _watchlist):
        return {"AAPL": self.frame("AAPL")}

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {sym: 100.0 for sym in symbols}

    def frame(self, symbol, *args, **kwargs):
        idx = pd.date_range("2026-07-17 09:30:00", periods=60, freq="min", tz="UTC")
        sym_upper = symbol.upper().lstrip("^")
        val = 15.0 if sym_upper == "VIX" else 100.0
        return pd.DataFrame(
            {"open": [val]*60, "high": [val]*60, "low": [val]*60, "close": [val]*60, "volume": [1000.0]*60},
            index=idx,
        )

    async def history(self, symbol, *args, **kwargs):
        idx = pd.date_range("2026-07-17 09:30:00", "2026-07-17 10:30:00", freq="min", tz="UTC")
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
        self.should_fail_account = False
        self.should_fail_positions = False
        self.mock_positions: list[Position] = []
        self.submitted_orders: list[Order] = []
        self.closed_positions: list[tuple[str, str]] = []

    async def connect(self): pass

    async def account(self):
        if self.should_fail_account:
            raise RuntimeError("Simulated account fetch failure")
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

    async def close_position(self, symbol: str, reason: str = "manual", qty: float | None = None, strategy: str | None = None, role: Any | None = None) -> Order | None:
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


@pytest.mark.asyncio
async def test_engine_reconciliation_disconnect_recovery(tmp_path, monkeypatch):
    """Verify that:
    1. An account/positions fetch failure during a tick marks reconciliation uncertain but does NOT crash the tick.
    2. The engine state remains RUNNING.
    3. reconciliation_failed remains True until broker queries succeed.
    4. Entries are blocked during this uncertain window.
    5. Re-attempting reconciliation on subsequent ticks works and successfully recovers.
    """
    settings = Settings(
        ALPHADESK_MODE="paper",
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
    monkeypatch.setattr("app.core.engine.make_broker", lambda name, settings: broker)

    await engine.start("paper")
    engine.clock.always_open = True

    # ── 1. Simulate account query failure ──────────────────────────────────
    broker.should_fail_account = True
    broker.should_fail_positions = False

    # Perform a tick. Should handle Exception, set reconciliation_failed, not crash.
    await engine._tick()

    assert engine.reconciliation_failed is True
    assert engine.state == EngineState.RUNNING

    # ── 2. Simulate positions query failure ────────────────────────────────
    broker.should_fail_account = False
    broker.should_fail_positions = True

    await engine._tick()

    assert engine.reconciliation_failed is True
    assert engine.state == EngineState.RUNNING

    # ── 3. Verify entries are blocked while reconciliation is uncertain ────
    strat = _FakeStrategy()
    engine.strategies = [strat]
    entry_sig = Signal(
        symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=1.0,
        strategy="test_strat", reason="test_entry",
        price=100.0, stop_loss=90.0, take_profit=120.0, ml_probability=0.9,
    )
    strat.signals_to_return.append(entry_sig)
    broker.submitted_orders.clear()

    await engine._tick()

    assert len(broker.submitted_orders) == 0, "Entries must be blocked when reconciliation is uncertain"

    # ── 4. Verify recovery when both queries succeed ───────────────────────
    broker.should_fail_account = False
    broker.should_fail_positions = False

    await engine._tick()

    assert engine.reconciliation_failed is False
    assert engine.state == EngineState.RUNNING
