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
        return {"AAPL": self.frame("AAPL", "1Min")}

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {sym: 100.0 for sym in symbols}

    def frame(self, symbol, *args, **kwargs):
        idx = pd.date_range("2026-07-17 09:30:00", periods=60, freq="min", tz="UTC")
        # Use low VIX value for VIX-like symbols to avoid PANIC market regime
        sym_upper = symbol.upper().lstrip("^")
        val = 15.0 if sym_upper == "VIX" else 100.0
        return pd.DataFrame(
            {"open": [val]*60, "high": [val]*60, "low": [val]*60, "close": [val]*60, "volume": [1000.0]*60},
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
        # Use low VIX value for both 'VIX' and '^VIX' to avoid PANIC market regime
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

    async def close_position(self, symbol: str, reason: str = "manual", qty: float | None = None) -> Order | None:
        self.closed_positions.append((symbol, reason))
        return Order(
            id=f"close-{symbol}",
            symbol=symbol,
            side=OrderSide.SELL,
            qty=qty if qty is not None else 10,
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
    """Bypass all pipeline ML/meta-label/trade-score gates: accept every signal."""
    def permissive_process(symbol, df, signals, regime, *args, **kwargs):
        accepted, rejected = [], []
        for sig in signals:
            sig.ml_probability = sig.ml_probability or 0.99
            sig.score = sig.score if sig.score is not None else 0.9
            accepted.append(sig)
        return accepted, rejected
    pipeline.process = permissive_process


def _patch_portfolio_gate_accept_all(engine):
    """Bypass portfolio correlation/sector gate: always approve."""
    engine.portfolio_gate.gate = lambda *args, **kwargs: (True, "ok")


def _patch_risk_accept_all(engine):
    """Bypass RiskManager gates: always approve with a minimal market order."""
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


@pytest.mark.asyncio
async def test_reconciliation_failure_blocks_entries_but_allows_exits(tmp_path, monkeypatch):
    """Verify the fail-closed reconciliation gate:
    - Exits always execute regardless of reconciliation state.
    - Entries are blocked while reconciliation_failed=True.
    - Entries resume normally once reconciliation_failed is cleared.
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
    broker.should_fail_positions = True  # Trigger startup reconciliation failure

    monkeypatch.setattr("app.core.engine.make_broker", lambda name, settings: broker)

    # ── 1. Start engine — reconciliation must fail ──────────────────────────
    await engine.start("paper")
    engine.clock.always_open = True  # Force market to be open during test to avoid EOD liquidation
    engine._last_scan_date = datetime.now(UTC).date()

    # Bypass pipeline gates, portfolio gate, and risk-manager entry checks so
    # only the reconciliation guard (engine-level) is exercised by this test.
    _patch_pipeline_accept_all(engine.pipeline)
    _patch_portfolio_gate_accept_all(engine)
    _patch_risk_accept_all(engine)

    assert engine.reconciliation_failed is True
    assert engine.status()["reconciliation_failed"] is True

    # ── 2. PIS EXIT triggers must still execute despite reconciliation failure ───
    strat = _FakeStrategy()
    engine.strategies = [strat]

    async def fake_pis_evaluate(*args, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(symbol="AAPL", qty=10, reason="take_profit_hit", order_type="market"), None

    engine.pis_adapter.evaluate_position_exit = fake_pis_evaluate

    broker.mock_positions = [Position(symbol="AAPL", qty=10, avg_entry_price=100.0, market_price=100.0)]
    broker.should_fail_positions = False

    real_reconcile = engine._reconcile_positions

    def fake_reconcile(positions):
        pass

    engine._reconcile_positions = fake_reconcile  # keep flag True

    await engine._tick()

    assert len(broker.closed_positions) == 1
    assert broker.closed_positions[0] == ("AAPL", "take_profit_hit")

    # ── 3. ENTRY signals must be BLOCKED while reconciliation_failed is True ─
    entry_sig = Signal(
        symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=1.0,
        strategy="test_strat", reason="test_entry",
        price=100.0, stop_loss=90.0, take_profit=120.0, ml_probability=0.9,
    )
    strat.signals_to_return.append(entry_sig)
    broker.submitted_orders.clear()

    await engine._tick()

    assert len(broker.submitted_orders) == 0, (
        "Entry order must be blocked while reconciliation_failed is True"
    )

    # ── 4. Restore reconciler — next tick clears the flag ──────────────────
    engine._reconcile_positions = real_reconcile
    engine.reconciliation_mismatch = False
    engine.system_health = "NORMAL"

    await engine._tick()
    assert engine.reconciliation_failed is False
    assert engine.status()["reconciliation_failed"] is False

    # ── 5. ENTRY signals must now be ACCEPTED ────────────────────────────────
    broker.mock_positions = []
    engine.portfolio._lots.clear()
    engine.reconciliation_mismatch = False
    engine.system_health = "NORMAL"
    engine.allow_new_entries = True
    strat.signals_to_return.append(entry_sig)
    broker.submitted_orders.clear()

    await engine._tick()

    assert len(broker.submitted_orders) == 1, (
        f"Expected 1 submitted order after reconciliation cleared, "
        f"got {len(broker.submitted_orders)}. reconciliation_failed={engine.reconciliation_failed}"
    )
    assert broker.submitted_orders[0].symbol == "AAPL"
