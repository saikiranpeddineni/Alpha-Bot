from __future__ import annotations

import sys
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pandas as pd

from app.config import Settings
from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position, Signal, SignalAction
from app.core.engine import TradingEngine, EngineState
from app.db.database import Database
from app.strategies.base import Strategy


class _FakeData:
    provider = SimpleNamespace(name="synthetic")
    async def warmup(self, _watchlist):
        return None
    async def poll(self, _watchlist):
        return {"AAPL": self.frame("AAPL", "1Min")}
    def frame(self, symbol, *args, **kwargs):
        import pandas as pd
        idx = pd.date_range("2026-07-17 09:30:00", periods=60, freq="min", tz="UTC")
        val = 15.0 if symbol.upper() == "VIX" else 100.0
        return pd.DataFrame({
            "open": [val]*60,
            "high": [val]*60,
            "low": [val]*60,
            "close": [val]*60,
            "volume": [1000.0]*60
        }, index=idx)
    async def history(self, symbol, *args, **kwargs):
        import pandas as pd
        start = kwargs.get("start") or (args[2] if len(args) > 2 else "2026-07-17 09:30:00")
        end = kwargs.get("end") or (args[3] if len(args) > 3 else "2026-07-17 10:30:00")
        start_ts = pd.Timestamp(start).tz_localize("UTC") if pd.Timestamp(start).tz is None else pd.Timestamp(start).tz_convert("UTC")
        end_ts = pd.Timestamp(end).tz_localize("UTC") if pd.Timestamp(end).tz is None else pd.Timestamp(end).tz_convert("UTC")
        idx = pd.date_range(start_ts, end_ts, freq="min")
        val = 15.0 if symbol.upper() == "VIX" else 100.0
        return pd.DataFrame({
            "open": [val]*len(idx),
            "high": [val]*len(idx),
            "low": [val]*len(idx),
            "close": [val]*len(idx),
            "volume": [1000.0]*len(idx)
        }, index=idx)


class _FakeBroker:
    name = "fake_broker"
    is_paper = True
    def __init__(self):
        self.should_fail_positions = False
        self.mock_positions = []
        self.submitted_orders = []
        self.closed_positions = []

    async def connect(self):
        pass
    async def account(self):
        return SimpleNamespace(equity=100_000.0, cash=100_000.0, buying_power=300_000.0)
    async def positions(self):
        if self.should_fail_positions:
            raise RuntimeError("Simulated positions fetch failure")
        return self.mock_positions
    async def open_orders(self):
        return []
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
        self.signals_to_return = []
    def on_bar(self, ctx, symbol, df):
        return self.signals_to_return.pop(0) if self.signals_to_return else None


@pytest.mark.asyncio
async def test_reconciliation_failure_blocks_entries_but_allows_exits(tmp_path, monkeypatch):
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
    broker.should_fail_positions = True  # Make startup reconciliation fail!

    # Override broker instance right after creation
    def fake_make_broker(name, settings):
        return broker
    monkeypatch.setattr("app.core.engine.make_broker", fake_make_broker)

    # 1. Start engine
    await engine.start("paper")

    # Now that modules are loaded, perform the broad sys.modules patch for latest_feature_row
    from app.ai.features import FEATURE_COLUMNS
    dummy_features = pd.Series({col: 0.0 for col in FEATURE_COLUMNS})
    for mod_name, mod in list(sys.modules.items()):
        if mod and hasattr(mod, "latest_feature_row"):
            monkeypatch.setattr(mod, "latest_feature_row", lambda df: dummy_features)

    # Mock model score_action and score_action_detailed to return high probability
    monkeypatch.setattr(engine.model, "score_action", lambda *args, **kwargs: 0.99)
    from app.ai.model import ScoreActionResult
    mock_res = ScoreActionResult(0.99, "BASE", "NO_ARTIFACT", None, 0.53, None)
    monkeypatch.setattr(engine.model, "score_action_detailed", lambda *args, **kwargs: mock_res)

    print("\n[TEST DEBUG] engine.model:", engine.model)
    print("[TEST DEBUG] engine.pipeline.model:", engine.pipeline.model)
    print("[TEST DEBUG] engine.model.score_action_detailed:", engine.model.score_action_detailed)
    print("[TEST DEBUG] engine.pipeline.model.score_action_detailed:", engine.pipeline.model.score_action_detailed)

    # Verify startup reconciliation failed and status reports it
    assert engine.reconciliation_failed is True
    status = engine.status()
    assert status["reconciliation_failed"] is True

    # 2. Inject a custom strategy to test signal evaluation
    strat = _FakeStrategy()
    engine.strategies = [strat]

    # Inject exit signal (exits must work!)
    exit_sig = Signal(
        symbol="AAPL",
        action=SignalAction.EXIT,
        confidence=1.0,
        strategy="test_strat",
        reason="take_profit_hit",
    )
    strat.signals_to_return.append(exit_sig)

    # Mock an active position for AAPL inside positions
    broker.mock_positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=100.0, market_price=100.0)
    ]
    broker.should_fail_positions = False
    
    # Save reference to real _reconcile_positions and mock it to do nothing
    real_reconcile = engine._reconcile_positions
    engine._reconcile_positions = lambda positions: None

    # Execute a tick:
    await engine._tick()

    # Verify that the strategy EXIT signal was successfully evaluated and submitted
    assert len(broker.closed_positions) == 1
    assert broker.closed_positions[0] == ("AAPL", "take_profit_hit")

    # 3. Test that entry signals are BLOCKED/GATED while reconciliation is failed (flag is still True)
    entry_sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=1.0,
        strategy="test_strat",
        reason="test_entry",
        price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        ml_probability=0.9,
    )
    strat.signals_to_return.append(entry_sig)

    broker.submitted_orders.clear()
    
    # Run tick
    await engine._tick()

    # Verify no order was submitted
    assert len(broker.submitted_orders) == 0

    # 4. Restore the real _reconcile_positions and clear reconciliation failed state (simulate successful tick reconciliation)
    engine._reconcile_positions = real_reconcile

    # We run another tick, since should_fail_positions is False, it should clear reconciliation_failed
    await engine._tick()
    assert engine.reconciliation_failed is False
    assert engine.status()["reconciliation_failed"] is False

    # Now verify entries are ACCEPTED and submitted after clearing active positions and lots
    broker.mock_positions = []
    engine.portfolio._lots.clear()
    strat.signals_to_return.append(entry_sig)
    
    print("\n[TEST DEBUG] reconciliation_failed BEFORE final tick:", engine.reconciliation_failed)
    try:
        await engine._tick()
    except Exception as exc:
        print("[TEST DEBUG] Exception during final tick:", exc)
        raise exc
    print("[TEST DEBUG] reconciliation_failed AFTER final tick:", engine.reconciliation_failed)
    
    assert len(broker.submitted_orders) == 1
    assert broker.submitted_orders[0].symbol == "AAPL"
