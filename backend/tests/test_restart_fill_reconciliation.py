from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

import pytest

from app.brokers.base import Fill
from app.brokers.alpaca import AlpacaBroker
from app.config import Settings
from app.core.domain import OrderSide
from app.core.domain import AccountSnapshot
from app.core.engine import TradingEngine
from app.db.database import Database
from app.portfolio.tracker import PortfolioTracker


def broker_fill(order_id: str, side: OrderSide, cumulative: float, price: float, *, symbol: str = "AAPL", ts=None, mode: str = "paper", account_id: str = "default_account") -> Fill:
    return Fill(
        order_id=order_id,
        symbol=symbol,
        side=side,
        qty=cumulative,
        price=price,
        strategy="restart-test",
        ts=ts or datetime.now(UTC),
        broker_fill_key=order_id,
        cumulative_qty=cumulative,
        broker_mode=mode,
        broker_account_id=account_id,
    )


def test_crash_restart_replays_unprocessed_fill_exactly_once(tmp_path):
    db = Database(tmp_path / "bot.db")
    fill = broker_fill("entry-1", OrderSide.BUY, 10, 100)
    restarted = PortfolioTracker(restore_mode="paper")
    restarted.on_fill(fill)
    assert restarted._lots["AAPL"].qty == 10
    assert db.processed_fill_qty(mode="paper", account_id="default_account", fill_key="entry-1") == 10
    restarted.on_fill(fill)
    assert restarted._lots["AAPL"].qty == 10
    second_restart = PortfolioTracker(restore_mode="paper")
    assert second_restart._lots["AAPL"].qty == 10
    second_restart.on_fill(fill)
    assert second_restart._lots["AAPL"].qty == 10


def test_partial_fill_watermarks_apply_only_monotonic_delta_in_order(tmp_path):
    Database(tmp_path / "bot.db")
    tracker = PortfolioTracker(restore_mode="paper")
    t0 = datetime(2026, 7, 15, 14, 30, tzinfo=UTC)
    tracker.on_fill(broker_fill("partial", OrderSide.BUY, 3, 100, ts=t0))
    tracker.on_fill(broker_fill("partial", OrderSide.BUY, 7, 102, ts=t0 + timedelta(seconds=1)))
    tracker.on_fill(broker_fill("partial", OrderSide.BUY, 10, 101, ts=t0 + timedelta(seconds=2)))
    assert tracker._lots["AAPL"].qty == 10
    assert tracker._lots["AAPL"].avg_price == pytest.approx((3 * 100 + 4 * 102 + 3 * 101) / 10)
    restarted = PortfolioTracker(restore_mode="paper")
    restarted.on_fill(broker_fill("partial", OrderSide.BUY, 10, 101, ts=t0 + timedelta(seconds=2)))
    assert restarted._lots["AAPL"].qty == 10


def test_long_exit_and_short_cover_restore_symmetrically(tmp_path):
    Database(tmp_path / "bot.db")
    tracker = PortfolioTracker(restore_mode="paper")
    t0 = datetime(2026, 7, 15, 14, 30, tzinfo=UTC)
    tracker.on_fill(broker_fill("long-entry", OrderSide.BUY, 5, 100, symbol="AAPL", ts=t0))
    long_trade = tracker.on_fill(broker_fill("long-exit", OrderSide.SELL, 5, 110, symbol="AAPL", ts=t0 + timedelta(minutes=1)))
    tracker.on_fill(broker_fill("short-entry", OrderSide.SELL, 4, 50, symbol="MSFT", ts=t0 + timedelta(minutes=2)))
    short_trade = tracker.on_fill(broker_fill("short-cover", OrderSide.BUY, 4, 45, symbol="MSFT", ts=t0 + timedelta(minutes=3)))
    assert long_trade is not None and long_trade.pnl == 50
    assert short_trade is not None and short_trade.pnl == 20
    assert tracker._lots == {}
    restarted = PortfolioTracker(restore_mode="paper")
    assert restarted._lots == {}
    assert restarted.realized_pnl == 70
    assert len(restarted.trades) == 2
    assert restarted.on_fill(broker_fill("long-exit", OrderSide.SELL, 5, 110, symbol="AAPL", ts=t0 + timedelta(minutes=1))) is None
    assert restarted.on_fill(broker_fill("short-cover", OrderSide.BUY, 4, 45, symbol="MSFT", ts=t0 + timedelta(minutes=3))) is None
    assert len(restarted.trades) == 2


def test_database_failure_rolls_back_in_memory_fill_application(tmp_path, monkeypatch):
    db = Database(tmp_path / "bot.db")
    tracker = PortfolioTracker(restore_mode="paper")
    fill = broker_fill("atomic", OrderSide.BUY, 8, 100)
    def fail_commit(**kwargs):
        raise RuntimeError("simulated durable commit failure")
    monkeypatch.setattr(db, "commit_fill_processing", fail_commit)
    with pytest.raises(RuntimeError, match="durable commit failure"):
        tracker.on_fill(fill)
    assert "AAPL" not in tracker._lots
    assert db.processed_fill_qty(mode="paper", account_id="default_account", fill_key="atomic") == 0


def test_checkpoint_and_fill_watermark_commit_together(tmp_path):
    db = Database(tmp_path / "bot.db")
    tracker = PortfolioTracker(restore_mode="paper")
    tracker.on_fill(broker_fill("entry", OrderSide.BUY, 6, 25))
    checkpoint = db.get_portfolio_checkpoint("paper")
    assert db.processed_fill_qty(mode="paper", account_id="default_account", fill_key="entry") == 6
    assert checkpoint is not None
    assert checkpoint["lots"]["AAPL"]["qty"] == 6


def test_restore_selects_only_explicit_broker_mode(tmp_path):
    db = Database(tmp_path / "bot.db")
    paper = PortfolioTracker(restore_mode="paper")
    paper.on_fill(broker_fill("paper-entry", OrderSide.BUY, 6, 25, symbol="AAPL", mode="paper"))
    live = PortfolioTracker(restore_mode="live")
    live.on_fill(broker_fill("live-entry", OrderSide.BUY, 3, 400, symbol="MSFT", mode="live"))
    assert db.get_portfolio_checkpoint("paper")["lots"]["AAPL"]["qty"] == 6
    assert db.get_portfolio_checkpoint("live")["lots"]["MSFT"]["qty"] == 3
    paper_restart = PortfolioTracker(restore_mode="paper")
    live_restart = PortfolioTracker(restore_mode="live")
    assert set(paper_restart._lots) == {"AAPL"}
    assert set(live_restart._lots) == {"MSFT"}


def test_non_broker_tracker_never_inherits_alpaca_checkpoint(tmp_path):
    Database(tmp_path / "bot.db")
    paper = PortfolioTracker(restore_mode="paper")
    paper.on_fill(broker_fill("paper-entry", OrderSide.BUY, 6, 25))
    isolated = PortfolioTracker()
    assert isolated._lots == {}
    assert isolated.trades == []
    assert isolated.realized_pnl == 0.0


def test_durable_watermarks_are_isolated_by_account_and_mode(tmp_path):
    db = Database(tmp_path / "bot.db")
    
    # 1. Fill order X under Paper Mode, Account A
    tracker_paper_a = PortfolioTracker(restore_mode="paper")
    tracker_paper_a.on_fill(broker_fill("order-x", OrderSide.BUY, 10, 100, mode="paper", account_id="account-a"))
    
    # 2. Verify it was written to paper / account-a
    assert db.processed_fill_qty(mode="paper", account_id="account-a", fill_key="order-x") == 10
    
    # 3. Verify it is NOT present in paper / account-b
    assert db.processed_fill_qty(mode="paper", account_id="account-b", fill_key="order-x") == 0.0
    
    # 4. Verify it is NOT present in live / account-a
    assert db.processed_fill_qty(mode="live", account_id="account-a", fill_key="order-x") == 0.0
    
    # 5. Process order X with a different quantity (5) under Paper Mode, Account B
    tracker_paper_b = PortfolioTracker(restore_mode="paper")
    tracker_paper_b.on_fill(broker_fill("order-x", OrderSide.BUY, 5, 100, mode="paper", account_id="account-b"))
    
    # 6. Verify they didn't contaminate each other
    assert db.processed_fill_qty(mode="paper", account_id="account-a", fill_key="order-x") == 10
    assert db.processed_fill_qty(mode="paper", account_id="account-b", fill_key="order-x") == 5


class _FakeData:
    provider = SimpleNamespace(name="synthetic")

    async def warmup(self, _watchlist):
        return None


class _FakePaperSimBroker:
    name = "paper_sim"
    _client = object()

    async def connect(self):
        return None

    async def positions(self):
        return []

    async def account(self):
        return AccountSnapshot(equity=100_000, cash=100_000, buying_power=100_000)

    async def open_orders(self):
        return []

    def drain_fills(self):
        return []

    async def cancel(self, _order_id):
        return True

    async def close_position(self, _symbol, reason="manual"):
        return None

    async def flatten_all(self, reason="flatten"):
        return []


class _FakeAlpacaBroker(_FakePaperSimBroker):
    name = "alpaca"
    account_id = "fake_acc_123"


async def test_engine_only_restores_durable_checkpoint_for_alpaca_backed_modes(tmp_path, monkeypatch):
    async def fake_ensure_data(self, mode=None):
        self.data = _FakeData()
        return self.data

    async def fake_run_loop(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(TradingEngine, "ensure_data", fake_ensure_data)
    monkeypatch.setattr(TradingEngine, "_run_loop", fake_run_loop)
    monkeypatch.setattr("app.brokers.alpaca.AlpacaBroker", _FakeAlpacaBroker)

    for mode, settings_kwargs, expected_restore in [
        ("demo", {"BROKER": "paper_sim"}, None),
        ("paper", {"BROKER": "alpaca", "ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"}, "paper"),
        (
            "live",
            {
                "BROKER": "alpaca",
                "ENABLE_LIVE_TRADING": True,
                "ALPACA_API_KEY": "key",
                "ALPACA_API_SECRET": "secret",
            },
            "live",
        ),
    ]:
        settings = Settings(
            ALPHADESK_MODE=mode,
            DATA_PROVIDER="synthetic",
            ALPHADESK_DB_PATH=str(tmp_path / f"{mode}.db"),
            DEFAULT_WATCHLIST="SPY,NVDA",
            DEMO_BAR_SECONDS=3600,
            **settings_kwargs,
        )
        db = Database(settings.resolved_db_path)
        engine = TradingEngine(settings, db)

        fake_broker = _FakeAlpacaBroker() if settings_kwargs.get("BROKER") == "alpaca" else _FakePaperSimBroker()
        monkeypatch.setattr("app.core.engine.make_broker", lambda name, _settings, broker=fake_broker: broker)

        await engine.start(mode, confirm="LIVE" if mode == "live" else "")
        assert engine.portfolio is not None
        assert engine.portfolio.restore_mode == expected_restore
        await engine.stop()


async def test_alpaca_positions_remain_broker_authoritative(tmp_path):
    db = Database(tmp_path / "alpaca.db")
    db.bootstrap_broker_checkpoint(
        mode="paper",
        account_id="fake-acc",
        checkpoint={
            "realized_pnl": 0.0,
            "day_realized_pnl": 0.0,
            "session_date": None,
            "session_start_equity": None,
            "peak_equity": 0.0,
            "day_trades_today": 0,
            "trades": [],
            "equity_curve": [],
            "lots": {"AAPL": {"qty": 999, "avg_price": 1.0, "entry_ts": datetime.now(UTC).isoformat(),
                               "strategy": "checkpoint", "stop_loss": None, "initial_risk_per_share": None}},
        },
        fill_watermarks={},
    )

    class _Client:
        def get_all_positions(self):
            return [
                SimpleNamespace(
                    symbol="AAPL",
                    qty="7",
                    avg_entry_price="101.5",
                    current_price="102.25",
                    side="PositionSide.LONG",
                )
            ]

    broker = object.__new__(AlpacaBroker)
    broker._client = _Client()
    broker._fills = []
    broker._meta = {}
    broker._symbol_strategies = {}
    broker.is_paper = True

    positions = await broker.positions()
    assert len(positions) == 1
    assert positions[0].qty == 7
    assert positions[0].avg_entry_price == 101.5
    assert positions[0].market_price == 102.25


def test_save_portfolio_checkpoint_persists_account_id(tmp_path):
    """Ensure save_portfolio_checkpoint commits account_id so future restores match."""
    from app.portfolio.tracker import PortfolioTracker
    
    db_path = tmp_path / "test_checkpoint.db"
    db = Database(db_path)
    
    # Save a checkpoint with account_id
    tracker1 = PortfolioTracker(restore_mode="paper", restore_account_id="PA3E1IKRFOCH")
    cp = tracker1.checkpoint(account_id="PA3E1IKRFOCH")
    db.save_portfolio_checkpoint("paper", cp)
    
    # Reload from DB into a fresh Database instance and instantiate new tracker
    db2 = Database(db_path)
    loaded_cp = db2.get_portfolio_checkpoint("paper")
    assert loaded_cp is not None
    assert loaded_cp.get("account_id") == "PA3E1IKRFOCH"
    
    # Tracker should restore cleanly without throwing/refusing
    tracker2 = PortfolioTracker(restore_mode="paper", restore_account_id="PA3E1IKRFOCH")
    assert tracker2.restore_account_id == "PA3E1IKRFOCH"

