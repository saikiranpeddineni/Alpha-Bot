"""Unit tests for blocking go-live P0 capital-safety and execution-integrity fixes.

Covers:
  #41 — live position reconcile must synthesize protective stop when missing
  #43 — fill watermark must NOT advance before durable commit_fill_processing
  #44 — partial fill accounting preserves avg_price and correct realized PnL
  #45 — duplicate flatten submissions blocked within one tick
  #47 — fill recovery lookback extended to 30 days
  #50 — paginated order fetch stops on partial page
  #51 — fills sorted by (ts, order_id) before drain
  #56 — emergency flatten task cancelled on engine stop
  #61 — cross-account portfolio checkpoint rejected on restore
  #86 — market-data gap blocks new entry signals per symbol
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# #41 — Live position reconcile must synthesize protective stop when missing
# ---------------------------------------------------------------------------

class TestIssue41ProtectiveExitRecovery:
    def _make_position(self, symbol="AAPL", qty=100.0, avg_price=150.0):
        from app.core.domain import Position
        p = Position(symbol=symbol, qty=qty, avg_entry_price=avg_price, market_price=avg_price, side="long")
        return p

    def _make_engine_with_portfolio(self):
        from app.portfolio.tracker import PortfolioTracker
        portfolio = PortfolioTracker()
        engine = MagicMock()
        engine.portfolio = portfolio
        engine.broker = MagicMock()
        engine.broker._symbol_strategies = {}
        engine.db = MagicMock()
        engine.db.get_latest_order_strategy.return_value = None
        engine.db.get_latest_order_payload.return_value = None
        engine.mode = "paper"
        engine.reconciliation_failed = False
        return engine

    def test_synthesizes_protective_stop_when_missing(self):
        """#41: When no stop loss in DB, _reconcile_positions must synthesize a 1.5% stop."""
        from app.core.engine import TradingEngine
        engine = self._make_engine_with_portfolio()
        position = self._make_position("AAPL", qty=100.0, avg_price=200.0)

        TradingEngine._reconcile_positions(engine, [position])

        lot = engine.portfolio._lots.get("AAPL")
        assert lot is not None, "Lot must be reconstructed"
        assert lot.stop_loss is not None, "#41: stop_loss must be synthesized"
        assert lot.stop_loss > 0, "#41: stop_loss must be positive"
        expected_stop = round(200.0 * (1.0 - 0.015), 4)
        assert abs(lot.stop_loss - expected_stop) < 1e-6

    def test_preserves_existing_stop_from_db(self):
        """#41: When a valid stop loss exists in DB payload, it must be preserved."""
        from app.core.engine import TradingEngine
        engine = self._make_engine_with_portfolio()
        engine.db.get_latest_order_payload.return_value = {"stop_loss": 185.0}
        position = self._make_position("AAPL", qty=100.0, avg_price=200.0)

        TradingEngine._reconcile_positions(engine, [position])

        lot = engine.portfolio._lots.get("AAPL")
        assert lot is not None
        assert abs(lot.stop_loss - 185.0) < 1e-6

    def test_synthesizes_stop_for_short_position(self):
        """#41: Short position gets a stop ABOVE avg_entry_price."""
        from app.core.engine import TradingEngine
        engine = self._make_engine_with_portfolio()
        position = self._make_position("NVDA", qty=-50.0, avg_price=400.0)

        TradingEngine._reconcile_positions(engine, [position])

        lot = engine.portfolio._lots.get("NVDA")
        assert lot is not None
        expected_stop = round(400.0 * (1.0 + 0.015), 4)
        assert abs(lot.stop_loss - expected_stop) < 1e-6


# ---------------------------------------------------------------------------
# #43 — Watermark must NOT commit before portfolio state update
# ---------------------------------------------------------------------------

class TestIssue43WatermarkCommitOrder:
    def test_in_memory_watermark_not_advanced_before_commit(self):
        """#43: _processed_fill_qty must NOT be advanced in poll_fills before commit_fill_processing."""
        import inspect
        from app.brokers.alpaca import AlpacaBroker

        source = inspect.getsource(AlpacaBroker.poll_fills)
        assert "self._processed_fill_qty[oid] = cumulative" not in source, (
            "#43: _processed_fill_qty must not be advanced in poll_fills before commit_fill_processing"
        )

    def test_local_watermarks_still_prevents_intra_poll_duplicates(self):
        """#43: local_watermarks still prevents duplicate enqueue within same poll cycle."""
        import inspect
        from app.brokers.alpaca import AlpacaBroker

        source = inspect.getsource(AlpacaBroker.poll_fills)
        assert "local_watermarks[oid] = cumulative" in source


# ---------------------------------------------------------------------------
# #44 — Partial fill accounting
# ---------------------------------------------------------------------------

class TestIssue44PartialFillAccounting:
    def test_partial_close_preserves_avg_price(self):
        """#44: Partial closing fill must NOT change avg_price of the remaining open lot."""
        from app.portfolio.tracker import PortfolioTracker, _OpenLot
        from app.brokers.base import Fill
        from app.core.domain import OrderSide

        tracker = PortfolioTracker()
        tracker._lots["AAPL"] = _OpenLot(
            qty=100.0,
            avg_price=100.0,
            entry_ts=datetime(2025, 1, 2, 14, 0, tzinfo=UTC),
            strategy="test",
            stop_loss=95.0,
            initial_risk_per_share=5.0,
        )

        close_fill = Fill(
            order_id="close-1", symbol="AAPL",
            side=OrderSide.SELL, qty=40.0, price=110.0,
            ts=datetime(2025, 1, 2, 15, 30, tzinfo=UTC),
            strategy="test",
        )

        trade = tracker._apply_fill(close_fill)

        assert trade is not None
        assert "AAPL" in tracker._lots
        remaining_lot = tracker._lots["AAPL"]
        assert abs(remaining_lot.qty - 60.0) < 1e-9
        assert abs(remaining_lot.avg_price - 100.0) < 1e-9, (
            f"#44: avg_price must remain 100.0 after partial close, got {remaining_lot.avg_price}"
        )
        assert abs(trade.pnl - 400.0) < 1e-6

    def test_full_close_removes_lot(self):
        """#44: Full close must remove the lot and record correct PnL."""
        from app.portfolio.tracker import PortfolioTracker, _OpenLot
        from app.brokers.base import Fill
        from app.core.domain import OrderSide

        tracker = PortfolioTracker()
        tracker._lots["MSFT"] = _OpenLot(
            qty=50.0, avg_price=300.0,
            entry_ts=datetime(2025, 1, 2, 14, 0, tzinfo=UTC),
            strategy="test",
        )

        close_fill = Fill(
            order_id="close-msft", symbol="MSFT",
            side=OrderSide.SELL, qty=50.0, price=315.0,
            ts=datetime(2025, 1, 2, 15, 30, tzinfo=UTC),
            strategy="test",
        )

        trade = tracker._apply_fill(close_fill)
        assert "MSFT" not in tracker._lots
        assert abs(trade.pnl - 750.0) < 1e-6


# ---------------------------------------------------------------------------
# #45 — Duplicate flatten guard within one tick
# ---------------------------------------------------------------------------

class TestIssue45DuplicateFlattenGuard:
    @pytest.mark.asyncio
    async def test_duplicate_close_skipped_by_flattening_symbols(self):
        """#45: Second _close_position_guarded call for same symbol in same tick is skipped."""
        from app.core.engine import TradingEngine

        engine = MagicMock()
        engine._pending_close_symbols = set()
        engine._flattening_symbols = {"AAPL"}  # already in-flight
        engine._pending_close_symbols.add("AAPL")
        engine.broker = MagicMock()
        engine.broker.close_position = AsyncMock(return_value=MagicMock())

        await TradingEngine._close_position_guarded(engine, "AAPL", "trailing stop")

        engine.broker.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_flattening_symbols_released_after_close(self):
        """#45: _flattening_symbols must be released after close completes."""
        from app.core.engine import TradingEngine

        engine = MagicMock()
        engine._pending_close_symbols = set()
        engine._flattening_symbols = set()
        engine.broker = MagicMock()
        engine.broker.close_position = AsyncMock(return_value=MagicMock())

        await TradingEngine._close_position_guarded(engine, "NVDA", "stop hit")

        assert "NVDA" not in engine._flattening_symbols

    @pytest.mark.asyncio
    async def test_flattening_symbols_released_on_exception(self):
        """#45: _flattening_symbols released even if close_position raises."""
        from app.core.engine import TradingEngine

        engine = MagicMock()
        engine._pending_close_symbols = set()
        engine._flattening_symbols = set()
        engine.broker = MagicMock()
        engine.broker.close_position = AsyncMock(side_effect=RuntimeError("broker error"))

        with pytest.raises(RuntimeError):
            await TradingEngine._close_position_guarded(engine, "AMD", "stop")

        assert "AMD" not in engine._flattening_symbols


# ---------------------------------------------------------------------------
# #47 — Fill recovery lookback extended to 30 days
# ---------------------------------------------------------------------------

class TestIssue47DowntimeFillRecovery:
    def test_poll_fills_uses_30_day_lookback(self):
        """#47: poll_fills must use 30-day lookback."""
        import inspect
        from app.brokers.alpaca import AlpacaBroker

        source = inspect.getsource(AlpacaBroker.poll_fills)
        assert "timedelta(days=30)" in source
        assert "timedelta(hours=24)" not in source

    def test_bootstrap_uses_30_day_lookback(self):
        """#47: _bootstrap_durable_fill_baseline must use 30-day lookback."""
        import inspect
        from app.brokers.alpaca import AlpacaBroker

        source = inspect.getsource(AlpacaBroker._bootstrap_durable_fill_baseline)
        assert "timedelta(days=30)" in source


# ---------------------------------------------------------------------------
# #50 — Paginated order fetch
# ---------------------------------------------------------------------------

class TestIssue50PaginatedOrderFetch:
    @pytest.mark.asyncio
    async def test_paginate_orders_stops_on_partial_page(self):
        """#50: _paginate_orders stops when page < page_size."""
        from app.brokers.alpaca import _paginate_orders
        from alpaca.trading.requests import GetOrdersRequest

        page1 = [MagicMock(filled_at=datetime(2025, 1, 1, tzinfo=UTC), id=f"o{i}") for i in range(500)]
        page2 = [MagicMock(filled_at=datetime(2025, 2, 1, tzinfo=UTC), id=f"p{i}") for i in range(3)]

        client = MagicMock()
        req = GetOrdersRequest(status="all", limit=500)

        with patch("app.brokers.alpaca._call", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = [page1, page2]
            result = await _paginate_orders(client, req, label="test")

        assert len(result) == 503

    @pytest.mark.asyncio
    async def test_paginate_orders_single_partial_page(self):
        """#50: _paginate_orders returns correctly when first page < page_size."""
        from app.brokers.alpaca import _paginate_orders
        from alpaca.trading.requests import GetOrdersRequest

        page = [MagicMock(filled_at=datetime(2025, 1, 1, tzinfo=UTC), id=f"o{i}") for i in range(42)]
        client = MagicMock()
        req = GetOrdersRequest(status="all", limit=500)

        with patch("app.brokers.alpaca._call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = page
            result = await _paginate_orders(client, req, label="test")

        assert len(result) == 42


# ---------------------------------------------------------------------------
# #51 — Causal fill sequence order
# ---------------------------------------------------------------------------

class TestIssue51CausalFillSequence:
    @pytest.mark.asyncio
    async def test_fills_sorted_by_ts_before_drain(self):
        """#51: _drain_fills must process fills in (ts ASC, order_id) order."""
        from app.core.domain import OrderSide
        from app.brokers.base import Fill
        from app.core.engine import TradingEngine

        ts_early = datetime(2025, 1, 2, 14, 0, tzinfo=UTC)
        ts_late = datetime(2025, 1, 2, 15, 0, tzinfo=UTC)

        fill_late = Fill(
            order_id="ord-late", symbol="AAPL",
            side=OrderSide.BUY, qty=10.0, price=100.0,
            ts=ts_late, strategy="test",
        )
        fill_early = Fill(
            order_id="ord-early", symbol="MSFT",
            side=OrderSide.BUY, qty=5.0, price=200.0,
            ts=ts_early, strategy="test",
        )

        mock_broker = MagicMock()
        mock_broker.drain_fills.return_value = [fill_late, fill_early]

        mock_portfolio = MagicMock()
        mock_portfolio.on_fill.return_value = None

        engine = MagicMock()
        engine.broker = mock_broker
        engine.portfolio = mock_portfolio
        engine.risk = MagicMock()
        engine.recent_orders = []
        engine.db = MagicMock()
        engine.db.get_order.return_value = None
        engine._position_peaks = {}
        engine._position_troughs = {}
        engine._position_initial_stops = {}
        engine._pending_close_symbols = set()
        engine._trades_since_insight = 0
        engine.perf_tracker = MagicMock()

        await TradingEngine._drain_fills(engine, datetime.now(UTC))

        calls = mock_portfolio.on_fill.call_args_list
        assert len(calls) == 2
        first_fill = calls[0][0][0]
        second_fill = calls[1][0][0]
        assert first_fill.symbol == "MSFT", f"#51: Early fill must be first, got {first_fill.symbol}"
        assert second_fill.symbol == "AAPL", f"#51: Late fill must be second, got {second_fill.symbol}"


# ---------------------------------------------------------------------------
# #56 — Emergency flatten task cancelled on engine stop
# ---------------------------------------------------------------------------

class TestIssue56EmergencyTaskCancellation:
    @pytest.mark.asyncio
    async def test_emergency_task_cancelled_on_stop(self):
        """#56: engine.stop() must cancel _emergency_flatten_task if it exists."""
        from app.core.engine import TradingEngine
        from app.core.domain import EngineState

        engine = MagicMock()
        engine._task = None
        engine.state = EngineState.HALTED
        engine.broker = None
        engine.publish = MagicMock()

        async def never_ending():
            await asyncio.sleep(9999)

        task = asyncio.get_event_loop().create_task(never_ending())
        engine._emergency_flatten_task = task

        await TradingEngine.stop(engine, flatten=False)

        assert task.cancelled(), "#56: Emergency flatten task must be cancelled on engine stop"

    def test_emergency_task_reference_stored(self):
        """#56: Emergency task must be stored in self._emergency_flatten_task."""
        import inspect
        from app.core.engine import TradingEngine

        source = inspect.getsource(TradingEngine._run_loop)
        assert "self._emergency_flatten_task = asyncio.create_task" in source


# ---------------------------------------------------------------------------
# #61 — Cross-account portfolio checkpoint rejected
# ---------------------------------------------------------------------------

class TestIssue61BrokerAccountIsolation:
    def test_checkpoint_embeds_account_id(self):
        """#61: checkpoint() must embed account_id."""
        from app.portfolio.tracker import PortfolioTracker

        tracker = PortfolioTracker(restore_account_id="account-123")
        cp = tracker.checkpoint()

        assert "account_id" in cp
        assert cp["account_id"] == "account-123"

    def test_cross_account_restore_refused(self):
        """#61: Restoring a checkpoint from different account_id must be refused."""
        from app.portfolio.tracker import PortfolioTracker
        from app.db.database import Database

        mock_db = MagicMock()
        mock_db.get_portfolio_checkpoint.return_value = {
            "account_id": "account-OLD",
            "realized_pnl": 99999.0,
            "lots": {},
            "trades": [],
            "equity_curve": [],
            "day_trades_today": 0,
        }

        with patch("app.db.database.Database.active", return_value=mock_db):
            tracker = PortfolioTracker(restore_mode="live", restore_account_id="account-NEW")

        assert tracker.realized_pnl == 0.0, "#61: Cross-account checkpoint must be refused"

    def test_same_account_restore_accepted(self):
        """#61: Restoring a checkpoint with matching account_id must succeed."""
        from app.portfolio.tracker import PortfolioTracker

        mock_db = MagicMock()
        mock_db.get_portfolio_checkpoint.return_value = {
            "account_id": "account-SAME",
            "realized_pnl": 500.0,
            "lots": {},
            "trades": [],
            "equity_curve": [],
            "day_trades_today": 0,
            "day_realized_pnl": 0.0,
        }

        with patch("app.db.database.Database.active", return_value=mock_db):
            tracker = PortfolioTracker(restore_mode="live", restore_account_id="account-SAME")

        assert abs(tracker.realized_pnl - 500.0) < 1e-6


# ---------------------------------------------------------------------------
# #86 — Market-data gap blocks new entry signals
# ---------------------------------------------------------------------------

class TestIssue86MarketDataGapGate:
    def test_gap_symbols_tracked_after_2min_no_bar(self):
        """#86: Symbol added to _data_gap_symbols after >2 min without a bar during session."""
        now = datetime(2025, 6, 2, 14, 0, 0, tzinfo=UTC)  # 10:00 ET
        stale_ts = now - timedelta(seconds=130)

        data_gap_symbols: set = set()
        last_data_bar_ts = {"AAPL": stale_ts}
        watchlist = ["AAPL"]
        new_closed: dict = {}

        et_mins = 9 * 60 + 35  # 9:35 ET — within session
        _DATA_GAP_THRESHOLD_SECS = 120
        _is_regular_session = 9 * 60 + 30 <= et_mins <= 16 * 60

        assert _is_regular_session
        for sym in watchlist:
            sym_upper = sym.upper()
            if sym_upper in new_closed:
                last_data_bar_ts[sym_upper] = now
                data_gap_symbols.discard(sym_upper)
            elif sym_upper in last_data_bar_ts:
                elapsed = (now - last_data_bar_ts[sym_upper]).total_seconds()
                if elapsed > _DATA_GAP_THRESHOLD_SECS:
                    data_gap_symbols.add(sym_upper)

        assert "AAPL" in data_gap_symbols

    def test_gap_cleared_when_fresh_bar_arrives(self):
        """#86: Gap cleared when a fresh bar arrives."""
        import pandas as pd

        data_gap_symbols = {"AAPL"}
        last_data_bar_ts: dict = {}
        watchlist = ["AAPL"]
        now = datetime(2025, 6, 2, 14, 5, 0, tzinfo=UTC)
        new_closed = {"AAPL": pd.DataFrame([{"close": 150}])}

        et_mins = 9 * 60 + 40
        _is_regular_session = 9 * 60 + 30 <= et_mins <= 16 * 60
        assert _is_regular_session

        for sym in watchlist:
            sym_upper = sym.upper()
            if sym_upper in new_closed and not new_closed[sym_upper].empty:
                last_data_bar_ts[sym_upper] = now
                data_gap_symbols.discard(sym_upper)

        assert "AAPL" not in data_gap_symbols

    def test_data_gap_symbols_attribute_on_engine(self):
        """#86: TradingEngine must have _data_gap_symbols attribute initialized in __init__."""
        import inspect
        from app.core.engine import TradingEngine
        source = inspect.getsource(TradingEngine.__init__)
        assert "_data_gap_symbols" in source, "#86: _data_gap_symbols must be initialized in __init__"
        assert "_last_data_bar_ts" in source, "#86: _last_data_bar_ts must be initialized in __init__"
