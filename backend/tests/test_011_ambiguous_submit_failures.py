from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.brokers.base import BrokerTimeoutError
from app.config import Settings
from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position
from app.core.engine import TradingEngine
from app.db.database import Database
from app.risk.manager import RiskConfig


class _FakeData:
    provider = SimpleNamespace(name="synthetic")
    async def warmup(self, _watchlist):
        return None
    async def poll(self, _watchlist):
        return {}
    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {sym: 100.0 for sym in symbols}
    def frame(self, *args, **kwargs):
        import pandas as pd
        idx = pd.date_range("2026-07-17 09:30:00", periods=5, freq="min", tz="UTC")
        return pd.DataFrame({
            "open": [100.0]*5,
            "high": [100.0]*5,
            "low": [100.0]*5,
            "close": [100.0]*5,
            "volume": [1000.0]*5
        }, index=idx)
    async def history(self, *args, **kwargs):
        import pandas as pd
        start = kwargs.get("start") or (args[2] if len(args) > 2 else "2026-07-17 09:30:00")
        end = kwargs.get("end") or (args[3] if len(args) > 3 else "2026-07-17 10:30:00")
        idx = pd.date_range(start, end, freq="min", tz="UTC")
        return pd.DataFrame({
            "open": [100.0]*len(idx),
            "high": [100.0]*len(idx),
            "low": [100.0]*len(idx),
            "close": [100.0]*len(idx),
            "volume": [1000.0]*len(idx)
        }, index=idx)


class _FakeBroker:
    name = "fake_broker"
    is_paper = True
    def __init__(self):
        self.submit_count = 0
        self.get_count = 0
        self.should_fail_submit = False
        self.should_fail_get = False
        self.mock_order_response = None
        self.orders = {}

    async def connect(self):
        pass
    async def account(self):
        return SimpleNamespace(equity=100_000.0, cash=100_000.0, buying_power=300_000.0)
    async def positions(self):
        return []
    async def open_orders(self):
        return []
    def drain_fills(self):
        return []

    async def submit(self, req: OrderRequest) -> Order:
        self.submit_count += 1
        if self.should_fail_submit:
            raise BrokerTimeoutError("Simulated ambiguous transport error")
        order = Order(
            id=req.client_order_id,
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            order_type=req.order_type,
            limit_price=req.limit_price,
            status=OrderStatus.SUBMITTED,
        )
        self.orders[req.client_order_id] = order
        return order

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        self.get_count += 1
        if self.should_fail_get:
            raise BrokerTimeoutError("Simulated get query timeout")
        return self.orders.get(client_order_id)


@pytest.mark.asyncio
async def test_ambiguous_submit_retains_pending_entry_guard_and_reconciles(tmp_path, monkeypatch):
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

    await engine.start("paper")
    broker = _FakeBroker()
    monkeypatch.setattr(engine, "broker", broker)

    client_order_id = "test-cid-123"
    engine.pending_reconciliation_orders[client_order_id] = {
        "symbol": "AAPL",
        "client_order_id": client_order_id,
        "side": "buy",
        "qty": 10.0,
        "order_type": "limit",
        "limit_price": 150.0,
        "submitted_at": datetime.now(UTC).isoformat(),
    }
    db.set_setting("pending_reconciliation_orders", engine.pending_reconciliation_orders)

    broker.should_fail_get = True
    await engine._tick()
    assert client_order_id in engine.pending_reconciliation_orders
    assert db.get_setting("pending_reconciliation_orders") != {}

    broker.should_fail_get = False
    fake_submitted_order = Order(
        id="broker-id-123",
        client_order_id=client_order_id,
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=10.0,
        order_type=OrderType.LIMIT,
        limit_price=150.0,
        status=OrderStatus.SUBMITTED,
    )
    broker.orders[client_order_id] = fake_submitted_order
    await engine._tick()

    assert client_order_id not in engine.pending_reconciliation_orders
    assert db.get_setting("pending_reconciliation_orders") == {}
    saved_payload = db.get_latest_order_payload("AAPL", "paper")
    assert saved_payload is not None
    assert saved_payload["status"] == "submitted"


@pytest.mark.asyncio
async def test_ambiguous_submit_single_not_found_retains_guard(tmp_path, monkeypatch):
    """One lookup miss is negative evidence, never definitive broker rejection."""
    settings = Settings(
        ALPHADESK_MODE="paper",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "bot.db"),
        DEFAULT_WATCHLIST="AAPL",
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
    await engine.start("paper")

    broker = _FakeBroker()
    monkeypatch.setattr(engine, "broker", broker)
    client_order_id = "test-cid-456"
    engine.pending_reconciliation_orders[client_order_id] = {
        "symbol": "AAPL",
        "client_order_id": client_order_id,
        "side": "buy",
        "qty": 10.0,
        "order_type": "limit",
        "limit_price": 150.0,
        "submitted_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    }
    db.set_setting("pending_reconciliation_orders", engine.pending_reconciliation_orders)

    await engine._reconcile_pending_submissions(now=datetime.now(UTC))

    assert client_order_id in engine.pending_reconciliation_orders
    assert engine.pending_reconciliation_orders[client_order_id]["reconcile_not_found_count"] == 1
    assert db.get_setting("pending_reconciliation_orders") != {}
    saved_payload = db.get_latest_order_payload("AAPL", "paper")
    assert saved_payload is None or saved_payload.get("status") != "rejected"


@pytest.mark.asyncio
async def test_ambiguous_submit_repeated_old_absence_resolves_to_rejection(tmp_path, monkeypatch):
    settings = Settings(
        ALPHADESK_MODE="paper",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "bot.db"),
        DEFAULT_WATCHLIST="AAPL",
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
    await engine.start("paper")
    broker = _FakeBroker()
    monkeypatch.setattr(engine, "broker", broker)

    cid = "test-cid-789"
    base = datetime.now(UTC)
    engine.pending_reconciliation_orders[cid] = {
        "symbol": "AAPL", "client_order_id": cid, "side": "buy", "qty": 10.0,
        "order_type": "limit", "limit_price": 150.0,
        "submitted_at": (base - timedelta(minutes=5)).isoformat(),
    }
    db.set_setting("pending_reconciliation_orders", engine.pending_reconciliation_orders)

    for seconds in (0, 1):
        await engine._reconcile_pending_submissions(now=base + timedelta(seconds=seconds))
        assert cid in engine.pending_reconciliation_orders
    await engine._reconcile_pending_submissions(now=base + timedelta(seconds=2))

    assert cid not in engine.pending_reconciliation_orders
    assert db.get_setting("pending_reconciliation_orders") == {}
    saved_payload = db.get_latest_order_payload("AAPL", "paper")
    assert saved_payload is not None
    assert saved_payload["status"] == "rejected"
