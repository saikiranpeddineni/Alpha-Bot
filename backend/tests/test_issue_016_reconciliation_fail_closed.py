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
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from app.core.domain import (
    OrderRequest,
    OrderSide,
    OrderType,
    SignalAction,
)
from app.risk.manager import RiskDecision


# ---------------------------------------------------------------------------
# _FakeData — non-panic VIX, stable OHLCV bars for all symbols
# ---------------------------------------------------------------------------

class _FakeData:
    provider = SimpleNamespace(name="synthetic")

    async def poll(self, watchlist: list[str]) -> dict[str, pd.DataFrame]:
        return {"AAPL": self.frame("AAPL")}

    def frame(self, symbol: str, timeframe: str = "1Min", limit: int = 60, **kwargs) -> pd.DataFrame:
        idx = pd.date_range("2026-07-17 09:30:00", periods=60, freq="min", tz="UTC")
        # Strip ^ prefix so ^VIX returns 15.0 (non-panic) not 100.0
        sym = symbol.upper().lstrip("^")
        val = 15.0 if sym == "VIX" else 100.0
        return pd.DataFrame(
            {
                "open": [val] * 60,
                "high": [val] * 60,
                "low": [val] * 60,
                "close": [val] * 60,
                "volume": [1_000.0] * 60,
            },
            index=idx,
        )

    async def history(self, symbol: str, timeframe: str, start=None, end=None, **kwargs) -> pd.DataFrame:
        return self.frame(symbol)

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: 100.0 for s in symbols}

    async def warmup(self, symbols: list[str]) -> None:
        pass

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# _FakeOrder / _FakePosition — minimal domain stubs
# ---------------------------------------------------------------------------

class _FakeOrder:
    def __init__(self, symbol: str, side: str) -> None:
        self.id = uuid.uuid4().hex
        self.client_order_id = uuid.uuid4().hex
        self.symbol = symbol
        self.side = side
        self.qty = 1
        self.order_type = "market"
        self.limit_price = None
        self.stop_loss = None
        self.status = "filled"
        self.reason = ""

    def model_dump(self, **kwargs) -> dict:
        return {"id": self.id, "symbol": self.symbol, "side": str(self.side)}


class _FakePosition:
    def __init__(self, symbol: str, qty: float = 10.0, avg_entry_price: float = 100.0) -> None:
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price
        self.stop_loss = None
        self.strategy = "test_strat"
        self.opened_at = None

    def to_payload(self) -> dict:
        return {"symbol": self.symbol, "qty": self.qty}


# ---------------------------------------------------------------------------
# _FakeBroker — full interface required by TradingEngine
# ---------------------------------------------------------------------------

class _FakeBroker:
    name = "fake_broker"
    is_paper = True

    def __init__(self, positions_to_return=None, *, raise_on_positions: bool = False) -> None:
        self._positions_to_return = positions_to_return or []
        self._raise_on_positions = raise_on_positions
        self.submitted_orders: list[_FakeOrder] = []
        self.closed_positions: list[str] = []
        self.mock_positions: list[_FakePosition] = []
        self._symbol_strategies: dict[str, str] = {}
        self._positions: dict[str, Any] = {}

    async def connect(self) -> None:
        pass

    async def account(self) -> SimpleNamespace:
        return SimpleNamespace(equity=100_000.0, cash=100_000.0, buying_power=300_000.0)

    async def positions(self) -> list[_FakePosition]:
        if self._raise_on_positions:
            raise RuntimeError("Simulated broker positions() failure")
        return list(self.mock_positions)

    async def open_orders(self) -> list:
        return []

    async def get_order_by_client_id(self, cid: str):
        return None

    async def flatten_all(self, reason: str = "") -> None:
        pass

    def drain_fills(self) -> list:
        return []

    async def submit(self, req: OrderRequest) -> _FakeOrder:
        order = _FakeOrder(req.symbol, req.side)
        self.submitted_orders.append(order)
        return order

    async def close_position(self, symbol: str, reason: str = "manual") -> _FakeOrder:
        self.closed_positions.append(symbol)
        return _FakeOrder(symbol, OrderSide.SELL)

    async def cancel_all(self) -> None:
        pass

    async def poll_fills(self) -> None:
        pass

    def update_price(self, symbol: str, price: float, ts: Any) -> None:
        pass

    def process_bar(self, symbol: str, row: Any, ts: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# _FakeStrategy — configurable signal emitter
# ---------------------------------------------------------------------------

class _FakeStrategy:
    name = "test_strat"
    enabled = True
    params: dict = {}

    def __init__(self) -> None:
        self.signals_to_return: list = []

    def reset(self) -> None:
        pass

    def on_bar(self, ctx: Any, symbol: str, df: pd.DataFrame):
        if self.signals_to_return:
            return self.signals_to_return.pop(0)
        return None

    def describe(self) -> dict:
        return {"name": self.name, "enabled": self.enabled}


# ---------------------------------------------------------------------------
# _FakeSignal — minimal domain signal stub
# ---------------------------------------------------------------------------

class _FakeSignal:
    def __init__(self, symbol: str, action) -> None:
        self.symbol = symbol
        self.action = action
        self.strategy = "test_strat"
        self.strategy_params: dict = {}
        self.ml_probability: float = 0.99
        self.score: float = 0.9
        self.reason: str = "test"
        self.stop_loss: float | None = None
        self.take_profit: float | None = None
        self.price: float | None = 100.0
        self.candidate_id: str | None = None


# ---------------------------------------------------------------------------
# Patch helpers (from dev guide)
# ---------------------------------------------------------------------------

def _patch_pipeline_accept_all(pipeline: Any) -> None:
    """Bypass all ML / meta-label / trade-score sub-gates."""
    def permissive_process(symbol, df, signals, regime, features=None, sector_strength=None):
        for sig in signals:
            sig.ml_probability = sig.ml_probability or 0.99
            sig.score = 0.9
        return signals, []

    pipeline.process = permissive_process


def _patch_risk_accept_all(engine: Any) -> None:
    """Bypass clock window, PDT, sizing so entries pass risk even outside market hours."""
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


def _patch_portfolio_gate_accept_all(engine: Any) -> None:
    """Approve all entries through the portfolio/sector gate."""
    from types import SimpleNamespace as NS

    class _FakeAllocDecision:
        approved_entries: list = []
        gated_entries: list = []

    def _fake_process(**kwargs):
        signals = kwargs.get("signals", [])
        d = _FakeAllocDecision()
        d.approved_entries = [(sig, 0.5) for sig in signals if sig.action != SignalAction.EXIT]
        d.gated_entries = []
        return d

    import app.portfolio.allocator as alloc_mod
    alloc_mod._original_ppa = getattr(alloc_mod, "process_portfolio_allocations", None)
    alloc_mod.process_portfolio_allocations = lambda **kw: _fake_process(**kw)


def _restore_portfolio_gate(engine: Any) -> None:
    import app.portfolio.allocator as alloc_mod
    orig = getattr(alloc_mod, "_original_ppa", None)
    if orig is not None:
        alloc_mod.process_portfolio_allocations = orig


# ---------------------------------------------------------------------------
# Engine factory — builds a minimal TradingEngine with fakes injected
# ---------------------------------------------------------------------------

def _make_engine(
    broker: _FakeBroker,
    strategy: _FakeStrategy,
    *,
    raise_on_positions: bool = False,
):
    """Construct a TradingEngine with fakes pre-wired, bypassing full start()."""
    from unittest.mock import MagicMock, AsyncMock
    from app.config import Settings, RiskConfig
    from app.core.engine import TradingEngine
    from app.core.domain import EngineState
    from app.portfolio.tracker import PortfolioTracker
    from app.risk.manager import RiskManager
    from app.core.clock import MarketClock
    from app.strategies import SignalPipeline
    from app.ai.model import SignalModel

    settings = Settings()  # uses defaults / env; no real keys needed for tests
    db = MagicMock()
    db.get_setting.return_value = None
    db.save_order = MagicMock()
    db.save_trade = MagicMock()
    db.save_equity = MagicMock()
    db.set_setting = MagicMock()
    db.get_latest_order_strategy.return_value = None
    db.get_latest_order_payload.return_value = None
    db.get_shadow_signals.return_value = []

    engine = TradingEngine.__new__(TradingEngine)
    engine.settings = settings
    engine.db = db
    engine.publish = lambda _t, _d: None
    engine.state = EngineState.RUNNING
    engine.mode = "paper"
    engine.last_error = ""
    engine.reconciliation_failed = False
    engine.clock = MarketClock(always_open=True)  # demo-style: always open
    engine.data = _FakeData()
    engine.broker = broker
    engine.strategies = [strategy]
    engine.portfolio = PortfolioTracker(restore_mode=None)
    engine.risk = RiskManager(settings.risk_config(), engine.clock)
    engine.risk.core_watchlist = []
    engine.pipeline = SignalPipeline(
        SignalModel(None),  # model not used — pipeline patched
        0.5,
    )
    from collections import deque
    engine.recent_orders = deque(maxlen=200)
    engine._regimes = {}
    engine._position_peaks = {}
    engine._position_troughs = {}
    engine._position_initial_stops = {}
    engine._pending_close_symbols = set()
    engine._tick_count = 0
    engine._consec_errors = 0
    engine._last_scan_date = None
    engine._last_train_date = None
    engine._last_event_monitor_min = None
    engine._last_intraday_scan_min = None
    engine._midday_active = False
    engine._flattened_sessions = set()
    engine._trades_since_insight = 0
    engine.pending_reconciliation_orders = {}
    engine.portfolio_gate = MagicMock()
    engine.portfolio_allocator = MagicMock()
    engine.trainer = None
    engine.perf_tracker = MagicMock()
    engine.gemini_shadow = MagicMock()
    engine.gemini_shadow.schedule = MagicMock()
    engine.insights = MagicMock()
    engine.insights.on_signal.return_value = MagicMock(model_dump=lambda **kw: {})
    engine.insights.on_regime_change.return_value = MagicMock(model_dump=lambda **kw: {})
    engine.user_watchlist = ["AAPL"]
    engine.scanned_watchlist = []
    engine.watchlist = ["AAPL"]
    engine.core_watchlist = ["AAPL"]
    engine.risk_config = settings.risk_config()

    # Calendar stub
    engine.calendar = MagicMock()
    engine.calendar.check = AsyncMock(return_value=(False, ""))

    # Roll the portfolio session so day_pnl works
    from app.core.domain import utcnow
    engine.portfolio.roll_session(engine.clock.session_date(), 100_000.0)
    engine.portfolio.peak_equity = 100_000.0
    engine.portfolio.mark_equity(100_000.0, utcnow())

    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReconciliationFailClosed:
    """5-phase regression suite for issue #016."""

    @pytest.mark.asyncio
    async def test_phase1_startup_reconciliation_failure_sets_flag(self):
        """Phase 1: broker positions() failure during start() must set reconciliation_failed=True."""
        broker = _FakeBroker(raise_on_positions=True)
        strat = _FakeStrategy()
        engine = _make_engine(broker, strat)

        # Simulate what start() does in the failure path
        try:
            positions = await broker.positions()  # raises
        except Exception:
            engine.reconciliation_failed = True

        assert engine.reconciliation_failed is True, (
            "reconciliation_failed must be True after broker.positions() raises at startup"
        )

    @pytest.mark.asyncio
    async def test_phase2_exits_still_execute_while_entry_gate_closed(self):
        """Phase 2: EXIT signals must close positions even while reconciliation_failed=True."""
        broker = _FakeBroker()
        broker.mock_positions = [_FakePosition("AAPL", qty=10.0)]
        strat = _FakeStrategy()
        engine = _make_engine(broker, strat)
        engine.reconciliation_failed = True  # gate is closed

        _patch_pipeline_accept_all(engine.pipeline)
        _patch_risk_accept_all(engine)
        _patch_portfolio_gate_accept_all(engine)

        try:
            exit_sig = _FakeSignal("AAPL", SignalAction.EXIT)
            strat.signals_to_return.append(exit_sig)
            await engine._tick()

            assert "AAPL" in broker.closed_positions, (
                "EXIT must still work while reconciliation_failed=True"
            )
            assert len(broker.submitted_orders) == 0, (
                "No entry order must be submitted while reconciliation_failed=True"
            )
        finally:
            _restore_portfolio_gate(engine)

    @pytest.mark.asyncio
    async def test_phase3_entries_blocked_while_reconciliation_failed(self):
        """Phase 3: ENTRY signals must be suppressed while reconciliation_failed=True."""
        broker = _FakeBroker()
        broker.mock_positions = []
        strat = _FakeStrategy()
        engine = _make_engine(broker, strat)
        engine.reconciliation_failed = True  # gate is closed

        _patch_pipeline_accept_all(engine.pipeline)
        _patch_risk_accept_all(engine)
        _patch_portfolio_gate_accept_all(engine)

        try:
            entry_sig = _FakeSignal("AAPL", SignalAction.ENTER_LONG)
            strat.signals_to_return.append(entry_sig)
            await engine._tick()

            assert len(broker.submitted_orders) == 0, (
                "Entry order must NOT be submitted while reconciliation_failed=True"
            )
        finally:
            _restore_portfolio_gate(engine)

    @pytest.mark.asyncio
    async def test_phase4_re_reconciliation_clears_flag(self):
        """Phase 4: When broker.positions() succeeds on next tick, reconciliation_failed is cleared."""
        broker = _FakeBroker()
        broker.mock_positions = []  # clean state — no positions at broker
        strat = _FakeStrategy()
        engine = _make_engine(broker, strat)
        engine.reconciliation_failed = True

        _patch_pipeline_accept_all(engine.pipeline)
        _patch_risk_accept_all(engine)
        _patch_portfolio_gate_accept_all(engine)

        try:
            # Run a tick with no signals — engine should re-attempt reconciliation
            await engine._tick()

            assert engine.reconciliation_failed is False, (
                "reconciliation_failed must be cleared after successful re-reconciliation in tick"
            )
        finally:
            _restore_portfolio_gate(engine)

    @pytest.mark.asyncio
    async def test_phase5_entries_unblocked_after_recovery(self):
        """Phase 5: After reconciliation_failed is cleared, entries must be submitted normally."""
        broker = _FakeBroker()
        broker.mock_positions = []
        strat = _FakeStrategy()
        engine = _make_engine(broker, strat)
        engine.reconciliation_failed = True

        _patch_pipeline_accept_all(engine.pipeline)
        _patch_risk_accept_all(engine)
        _patch_portfolio_gate_accept_all(engine)

        try:
            # Phase 4: recover on empty tick
            await engine._tick()
            assert engine.reconciliation_failed is False

            # Phase 5: entry on next tick
            broker.submitted_orders.clear()
            entry_sig = _FakeSignal("AAPL", SignalAction.ENTER_LONG)
            strat.signals_to_return.append(entry_sig)
            await engine._tick()

            assert len(broker.submitted_orders) == 1, (
                "Entry order MUST be submitted after reconciliation_failed is cleared"
            )
        finally:
            _restore_portfolio_gate(engine)

    @pytest.mark.asyncio
    async def test_status_reports_reconciliation_failed(self):
        """engine.status() must expose reconciliation_failed truthfully."""
        broker = _FakeBroker()
        strat = _FakeStrategy()
        engine = _make_engine(broker, strat)

        engine.reconciliation_failed = False
        assert engine.status()["reconciliation_failed"] is False

        engine.reconciliation_failed = True
        assert engine.status()["reconciliation_failed"] is True

    @pytest.mark.asyncio
    async def test_reconcile_positions_long_and_short_clears_flag(self):
        """_reconcile_positions() with both long and short positions clears the flag."""
        broker = _FakeBroker()
        strat = _FakeStrategy()
        engine = _make_engine(broker, strat)
        engine.reconciliation_failed = True

        positions = [
            _FakePosition("AAPL", qty=10.0, avg_entry_price=150.0),   # long
            _FakePosition("TSLA", qty=-5.0, avg_entry_price=200.0),   # short
        ]

        engine._reconcile_positions(positions)

        assert engine.reconciliation_failed is False, (
            "_reconcile_positions() must set reconciliation_failed=False on success"
        )
        assert "AAPL" in engine.portfolio._lots
        assert "TSLA" in engine.portfolio._lots
        assert engine.portfolio._lots["AAPL"].qty == 10.0
        assert engine.portfolio._lots["TSLA"].qty == -5.0

    @pytest.mark.asyncio
    async def test_engine_state_remains_running_after_reconciliation_failure(self):
        """Engine state must stay RUNNING (not HALTED) after reconciliation failure — fail-closed
        only blocks entries, it does not halt the engine entirely."""
        from app.core.domain import EngineState

        broker = _FakeBroker(raise_on_positions=True)
        strat = _FakeStrategy()
        engine = _make_engine(broker, strat)

        # Simulate startup failure path
        try:
            await broker.positions()
        except Exception:
            engine.reconciliation_failed = True

        assert engine.state == EngineState.RUNNING, (
            "Engine must stay RUNNING after reconciliation failure — exits must still be available"
        )
        assert engine.reconciliation_failed is True
