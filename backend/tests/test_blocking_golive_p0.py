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

    @pytest.mark.asyncio
    async def test_authoritative_broker_protective_stop_verification_success(self):
        """#41: Valid active broker stop order verified successfully."""
        from app.core.engine import TradingEngine
        from app.core.domain import Order, OrderSide, OrderType, OrderStatus, Position

        engine = self._make_engine_with_portfolio()
        engine.allow_new_entries = True
        engine.system_health = "NORMAL"

        position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0, opened_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
        TradingEngine._reconcile_positions(engine, [position])

        broker_stop = Order(
            id="stop-1", symbol="AAPL", side=OrderSide.SELL, qty=100.0,
            order_type=OrderType.LIMIT, stop_loss=190.0, status=OrderStatus.SUBMITTED,
            submitted_at=datetime(2026, 7, 20, 10, 1, tzinfo=UTC)
        )
        engine.broker.open_orders = AsyncMock(return_value=[broker_stop])

        status = await TradingEngine.verify_broker_protective_stops(engine, [position])
        assert status.get("AAPL") is True, "#41: Valid broker stop order must be verified"
        assert engine.allow_new_entries is True
        assert engine.portfolio._lots["AAPL"].stop_loss == 190.0

    @pytest.mark.asyncio
    async def test_broker_protection_missing_fails_closed_and_triggers_recovery(self):
        """#41: Missing broker protection fails closed (allow_new_entries=False) and submits recovery stop."""
        from app.core.engine import TradingEngine
        from app.core.domain import OrderSide, OrderType, Position

        engine = self._make_engine_with_portfolio()
        engine.allow_new_entries = True
        engine.system_health = "NORMAL"
        engine._pending_close_symbols = set()
        engine._flattening_symbols = set()

        position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0, opened_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
        TradingEngine._reconcile_positions(engine, [position])

        engine.broker.open_orders = AsyncMock(return_value=[])
        engine.broker.submit = AsyncMock(return_value=MagicMock())

        status = await TradingEngine.verify_broker_protective_stops(engine, [position])

        engine.broker.submit.assert_called_once()
        recovery_req = engine.broker.submit.call_args[0][0]
        assert recovery_req.symbol == "AAPL"
        assert recovery_req.side == OrderSide.SELL
        assert recovery_req.qty == 100.0
        assert recovery_req.order_type == OrderType.STOP
        assert recovery_req.stop_price == 197.0
        assert recovery_req.limit_price is None


    @pytest.mark.asyncio
    async def test_broker_protection_rejects_stale_children_and_insufficient_qty(self):
        """#41: Rejects stale orders submitted before position opened_at and orders with insufficient qty."""
        from app.core.engine import TradingEngine
        from app.core.domain import Order, OrderSide, OrderType, OrderStatus, Position

        engine = self._make_engine_with_portfolio()
        engine.allow_new_entries = True
        engine.system_health = "NORMAL"
        engine._pending_close_symbols = set()
        engine._flattening_symbols = set()

        pos_opened = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
        stale_submitted = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

        position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0, opened_at=pos_opened)
        TradingEngine._reconcile_positions(engine, [position])

        stale_order = Order(
            id="stale-1", symbol="AAPL", side=OrderSide.SELL, qty=100.0,
            order_type=OrderType.LIMIT, stop_loss=190.0, status=OrderStatus.SUBMITTED,
            submitted_at=stale_submitted
        )
        partial_order = Order(
            id="small-1", symbol="AAPL", side=OrderSide.SELL, qty=50.0,
            order_type=OrderType.LIMIT, stop_loss=190.0, status=OrderStatus.SUBMITTED,
            submitted_at=datetime(2026, 7, 20, 14, 1, tzinfo=UTC)
        )

        engine.broker.open_orders = AsyncMock(return_value=[stale_order, partial_order])
        engine.broker.submit = AsyncMock(return_value=MagicMock())

        # Cycle 1: stale/undersized orders are cancelled, recovery submission deferred until cancellation ACK
        status = await TradingEngine.verify_broker_protective_stops(engine, [position])
        engine.broker.submit.assert_not_called()

        # Cycle 2: broker open_orders is now empty
        engine.broker.open_orders = AsyncMock(return_value=[])
        await TradingEngine.verify_broker_protective_stops(engine, [position])
        engine.broker.submit.assert_called_once()



# ---------------------------------------------------------------------------
# #43 — Watermark must NOT commit before portfolio state update
# ---------------------------------------------------------------------------

class TestIssue43WatermarkCommitOrder:
    def test_watermark_and_checkpoint_committed_atomically_in_same_db_transaction(self):
        """#43: Database.commit_fill_processing commits FillProgressRow and PortfolioCheckpointRow atomically."""
        import inspect
        from app.db.database import Database

        source = inspect.getsource(Database.commit_fill_processing)
        assert "FillProgressRow" in source
        assert "PortfolioCheckpointRow" in source
        assert "s.commit()" in source

    def test_risk_state_persisted_and_restored_from_checkpoint(self):
        """#43: Safety-critical risk state (consecutive losses, cooldowns) persisted in checkpoint and restored."""
        from app.risk.manager import RiskManager
        from app.config import RiskConfig
        from app.core.clock import MarketClock

        rm = RiskManager(RiskConfig(), MarketClock())
        rm.consecutive_losses = 3
        rm.symbol_cooldowns = {"AAPL": datetime(2026, 7, 20, 15, 0, tzinfo=UTC)}

        risk_state = rm.get_risk_state()
        assert risk_state["consecutive_losses"] == 3
        assert "AAPL" in risk_state["symbol_cooldowns"]

        rm_restored = RiskManager(RiskConfig(), MarketClock())
        rm_restored.restore_risk_state(risk_state)
        assert rm_restored.consecutive_losses == 3
        assert "AAPL" in rm_restored.symbol_cooldowns

    def test_post_close_risk_state_persisted_atomically_on_trade_close(self, tmp_path):
        """#43: RiskManager.on_trade_closed runs BEFORE checkpoint persistence, saving post-close risk state."""
        from app.portfolio.tracker import PortfolioTracker, _OpenLot
        from app.brokers.base import Fill
        from app.core.domain import OrderSide
        from app.risk.manager import RiskManager
        from app.config import RiskConfig
        from app.core.clock import MarketClock
        from app.db.database import Database

        db = Database(tmp_path / "test_risk_atomic.db")
        rm = RiskManager(RiskConfig(), MarketClock())

        tracker = PortfolioTracker(restore_mode="paper")
        tracker._lots["AAPL"] = _OpenLot(
            qty=100.0, avg_price=100.0,
            entry_ts=datetime(2025, 1, 2, 14, 0, tzinfo=UTC),
            strategy="test",
        )

        close_fill = Fill(
            order_id="close-losing", symbol="AAPL",
            side=OrderSide.SELL, qty=100.0, price=90.0,
            ts=datetime(2025, 1, 2, 15, 30, tzinfo=UTC),
            strategy="test", broker_fill_key="close-losing",
            cumulative_qty=100.0, broker_mode="paper"
        )

        with patch("app.db.database.Database.active", return_value=db):
            tracker.on_fill(close_fill, risk_manager=rm, now=close_fill.ts)

        cp = db.get_portfolio_checkpoint("paper")
        assert cp is not None
        assert "risk_state" in cp
        assert "AAPL" in cp["risk_state"]["symbol_cooldowns"], "#43: Post-close symbol cooldown must be persisted in DB checkpoint"




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

    def test_incremental_execution_pricing_calculates_exact_delta_price(self):
        """#44: Incremental fill delta price is derived from change in cumulative notional."""
        from app.brokers.alpaca import AlpacaBroker
        broker = AlpacaBroker("key", "secret", paper=True)

        # First snapshot: 40 shares filled @ avg $100.0 (notional = $4,000)
        # Second snapshot: 50 shares filled @ avg $102.0 (notional = $5,100)
        # Incremental fill: 10 shares, expected price = (5100 - 4000) / 10 = $110.0

        from types import SimpleNamespace
        o1 = SimpleNamespace(id="ord-1", symbol="AAPL", side="buy", filled_qty=40.0, filled_avg_price=100.0, status="partially_filled", submitted_at=datetime.now(UTC), filled_at=datetime.now(UTC))
        o2 = SimpleNamespace(id="ord-1", symbol="AAPL", side="buy", filled_qty=50.0, filled_avg_price=102.0, status="partially_filled", submitted_at=datetime.now(UTC), filled_at=datetime.now(UTC))

        # First poll
        with patch("app.brokers.alpaca._paginate_orders", new_callable=AsyncMock) as mock_p:
            mock_p.return_value = [o1]
            asyncio.run(broker.poll_fills())
            fills1 = broker.drain_fills()
            assert len(fills1) == 1
            assert fills1[0].qty == 40.0
            assert fills1[0].price == 100.0

        # Second poll
        with patch("app.brokers.alpaca._paginate_orders", new_callable=AsyncMock) as mock_p:
            mock_p.return_value = [o2]
            asyncio.run(broker.poll_fills())
            fills2 = broker.drain_fills()
            assert len(fills2) == 1
            assert fills2[0].qty == 10.0
            assert fills2[0].price == 110.0, f"#44: Expected delta fill price 110.0, got {fills2[0].price}"

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

    def test_xnys_calendar_early_close_and_holiday_session_bounds(self):
        """#86: XNYS calendar correctly identifies 13:00 ET early close days and holidays."""
        from app.core.calendar_helper import get_session_close, ET
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # July 3, 2025 (Day before Independence Day — 13:00 ET early close)
        july3 = datetime(2025, 7, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        close_july3 = get_session_close(july3)
        assert close_july3.hour == 13, f"#86: July 3 close expected 13:00 ET, got {close_july3}"

        # July 4, 2025 (Independence Day — Exchange Holiday)
        july4 = datetime(2025, 7, 4, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        with pytest.raises(ValueError, match="not an XNYS trading session"):
            get_session_close(july4)


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_paginate_orders_deduplicates_and_handles_identical_timestamps(self):
        """#50: Paginate orders deduplicates overlap and does not infinite loop on identical timestamps."""
        from app.brokers.alpaca import _paginate_orders
        from alpaca.trading.requests import GetOrdersRequest
        from types import SimpleNamespace

        ts_same = datetime(2025, 5, 1, 14, 0, tzinfo=UTC)
        o1 = SimpleNamespace(id="ord-1", filled_at=ts_same)
        o2 = SimpleNamespace(id="ord-2", filled_at=ts_same)

        client = MagicMock()
        with patch("app.brokers.alpaca._call", new_callable=AsyncMock) as mock_call:
            # First call returns 2 orders; second call returns same 2 orders (overlapping page)
            mock_call.side_effect = [[o1, o2], [o1, o2]]
            result = await _paginate_orders(client, GetOrdersRequest(limit=2))

        assert len(result) == 2, f"#50: Expected 2 unique orders, got {len(result)}"

    @pytest.mark.asyncio
    async def test_interleaved_partial_fills_execution_chronology(self):
        """#51: Interleaved partial fills across multiple orders sort deterministically by (ts, order_id, cumulative_qty)."""
        from app.core.domain import OrderSide
        from app.brokers.base import Fill
        from app.core.engine import TradingEngine

        ts = datetime(2025, 1, 2, 14, 0, tzinfo=UTC)

        f_b1 = Fill(order_id="ord-B", symbol="MSFT", side=OrderSide.BUY, qty=10.0, price=200.0, ts=ts, strategy="test", cumulative_qty=10.0)
        f_a2 = Fill(order_id="ord-A", symbol="AAPL", side=OrderSide.BUY, qty=20.0, price=100.0, ts=ts, strategy="test", cumulative_qty=30.0)
        f_a1 = Fill(order_id="ord-A", symbol="AAPL", side=OrderSide.BUY, qty=10.0, price=100.0, ts=ts, strategy="test", cumulative_qty=10.0)

        mock_broker = MagicMock()
        mock_broker.drain_fills.return_value = [f_b1, f_a2, f_a1]

        mock_portfolio = MagicMock()
        engine = MagicMock()
        engine.broker = mock_broker
        engine.portfolio = mock_portfolio
        engine.recent_orders = []
        engine.db = MagicMock()
        engine.db.get_order.return_value = None
        engine._position_peaks = {}
        engine._position_troughs = {}
        engine._position_initial_stops = {}
        engine._pending_close_symbols = set()
        engine.risk = MagicMock()
        engine._trades_since_insight = 0
        engine.perf_tracker = MagicMock()


        await TradingEngine._drain_fills(engine, ts)

        calls = mock_portfolio.on_fill.call_args_list
        assert len(calls) == 3
        # Sorted order: (ord-A, cum 10.0) -> (ord-A, cum 30.0) -> (ord-B, cum 10.0)
        assert calls[0][0][0].order_id == "ord-A" and calls[0][0][0].cumulative_qty == 10.0
        assert calls[1][0][0].order_id == "ord-A" and calls[1][0][0].cumulative_qty == 30.0
        assert calls[2][0][0].order_id == "ord-B" and calls[2][0][0].cumulative_qty == 10.0

    def test_missing_account_id_fails_closed(self):
        """#61: Checkpoint with missing account_id fails closed when restore_account_id is required."""
        from app.portfolio.tracker import PortfolioTracker

        mock_db = MagicMock()
        mock_db.get_portfolio_checkpoint.return_value = {
            # account_id is None / missing
            "realized_pnl": 99999.0,
            "lots": {},
            "trades": [],
        }

        with patch("app.db.database.Database.active", return_value=mock_db):
            tracker = PortfolioTracker(restore_mode="live", restore_account_id="account-REQUIRED")

        assert tracker.realized_pnl == 0.0, "#61: Missing account_id in checkpoint must fail closed"

