from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import Settings
from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position, Signal, SignalAction
from app.core.engine import TradingEngine
from app.db.database import Database
from app.risk.manager import RiskDecision
from app.strategies.base import Strategy


class _FakeData:
    provider = SimpleNamespace(name="synthetic")

    async def warmup(self, _watchlist):
        return None

    def last_price(self, symbol):
        sym_upper = symbol.upper().lstrip("^")
        return 15.0 if sym_upper == "VIX" else 100.0

    def last_prices(self, symbols):
        return {s: self.last_price(s) for s in symbols}

    async def poll(self, _watchlist):
        return {"AAPL": self.frame("AAPL", "1Min")}

    def frame(self, symbol, *args, **kwargs):
        idx = pd.date_range("2026-07-17 09:30:00", periods=60, freq="min", tz="UTC")
        sym_upper = symbol.upper().lstrip("^")
        val = 15.0 if sym_upper == "VIX" else 100.0
        return pd.DataFrame(
            {"open": [val] * 60, "high": [val] * 60, "low": [val] * 60, "close": [val] * 60, "volume": [1000.0] * 60},
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
            {"open": [val] * len(idx), "high": [val] * len(idx), "low": [val] * len(idx), "close": [val] * len(idx), "volume": [10000.0] * len(idx)},
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


@pytest.fixture
async def engine_with_failed_reconciliation(tmp_path, monkeypatch):
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
    broker.should_fail_positions = True
    monkeypatch.setattr("app.core.engine.make_broker", lambda name, settings: broker)

    await engine.start("demo")

    _patch_pipeline_accept_all(engine.pipeline)
    _patch_portfolio_gate_accept_all(engine)
    _patch_risk_accept_all(engine)

    strat = _FakeStrategy()
    engine.strategies = [strat]

    return engine, broker, strat


@pytest.mark.asyncio
async def test_reconciliation_failure_sets_flag(engine_with_failed_reconciliation):
    engine, broker, strat = engine_with_failed_reconciliation
    assert engine.reconciliation_failed is True
    assert engine.status()["reconciliation_failed"] is True


@pytest.mark.asyncio
async def test_exits_work_while_reconciliation_failed(engine_with_failed_reconciliation):
    """Exit path (_close_position_guarded) works even when reconciliation_failed=True.
    
    PIS exclusively owns exits via _close_position_guarded. This verifies the exit
    route is not blocked by the reconciliation entry gate.
    """
    engine, broker, strat = engine_with_failed_reconciliation
    assert engine.reconciliation_failed is True, "Fixture must establish failed state"

    broker.mock_positions = [Position(symbol="AAPL", qty=10, avg_entry_price=100.0, market_price=100.0)]
    broker.should_fail_positions = False

    # Seed the portfolio lot
    engine._reconcile_positions(broker.mock_positions)
    engine.reconciliation_failed = True  # restore — reconcile clears the flag

    # Call the actual PIS exit path
    await engine._close_position_guarded("AAPL", reason="take_profit_hit", qty=10)

    assert len(broker.closed_positions) == 1
    assert broker.closed_positions[0][0] == "AAPL"
    assert len(broker.submitted_orders) == 0


@pytest.mark.asyncio
async def test_entries_blocked_while_reconciliation_failed(engine_with_failed_reconciliation):
    engine, broker, strat = engine_with_failed_reconciliation

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
    broker.mock_positions = []
    broker.should_fail_positions = False

    real_reconcile = engine._reconcile_positions
    from unittest.mock import MagicMock
    engine._reconcile_positions = MagicMock()

    await engine._tick()

    engine._reconcile_positions = real_reconcile

    assert len(broker.submitted_orders) == 0


@pytest.mark.asyncio
async def test_successful_tick_clears_reconciliation_flag(engine_with_failed_reconciliation):
    engine, broker, strat = engine_with_failed_reconciliation
    broker.should_fail_positions = False
    broker.mock_positions = []

    await engine._tick()

    assert engine.reconciliation_failed is False
    assert engine.status()["reconciliation_failed"] is False


@pytest.mark.asyncio
async def test_entries_unblocked_after_recovery(engine_with_failed_reconciliation):
    engine, broker, strat = engine_with_failed_reconciliation

    broker.should_fail_positions = False
    broker.mock_positions = []
    await engine._tick()

    entry_sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=1.0,
        strategy="test_strat",
        reason="recovered_entry",
        price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        ml_probability=0.9,
    )
    strat.signals_to_return.append(entry_sig)
    engine.system_health = "NORMAL"
    engine.vix_unavailable = False
    engine.risk.clock.always_open = True

    await engine._tick()

    assert engine.reconciliation_failed is False
    assert len(broker.submitted_orders) == 1


@pytest.mark.asyncio
async def test_unbound_local_regression_when_reconciliation_failed_and_approved_entries(engine_with_failed_reconciliation):
    engine, broker, strat = engine_with_failed_reconciliation

    # Set reconciliation_failed to True
    engine.reconciliation_failed = True

    # Setup approved entries in the decision outcome
    from app.portfolio.allocator import PortfolioDecision
    
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
        candidate_id="test_candidate_123",
    )
    
    outcome = PortfolioDecision(
        approved_entries=[(entry_sig, 1.0)],
        gated_entries=[]
    )
    
    # Mock evaluate to return this outcome
    from unittest.mock import MagicMock
    engine.portfolio_gate.evaluate = MagicMock(return_value=outcome)
    
    # Run _tick and verify no crash/UnboundLocalError occurs
    broker.should_fail_positions = False
    broker.mock_positions = []
    
    await engine._tick()
    
    # No broker submissions should occur
    assert len(broker.submitted_orders) == 0

