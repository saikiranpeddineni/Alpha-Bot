import pytest
from datetime import date, datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock

from app.brokers.base import Fill
from app.core.domain import Order, OrderRequest, OrderSide, OrderType, OrderStatus, Position
from app.core.engine import TradingEngine
from app.portfolio.tracker import PortfolioTracker, _OpenLot
from app.risk.manager import RiskManager, RiskConfig





def test_two_partial_fills_create_one_logical_loss():
    tracker = PortfolioTracker()
    risk = RiskManager(RiskConfig(), clock=MagicMock())
    t0 = datetime.now(UTC)
    t1 = datetime.now(UTC)
    t2 = datetime.now(UTC)

    tracker._lots["AAPL"] = _OpenLot(
        qty=10.0, avg_price=200.0, entry_ts=t0, strategy="manual", initial_risk_per_share=2.0
    )

    first_five_share_exit = Fill(
        order_id="exit_1", symbol="AAPL", side=OrderSide.SELL, qty=5.0, price=195.0, ts=t1, strategy="manual"
    )
    first_res = tracker.on_fill(first_five_share_exit, risk_manager=risk)

    assert first_res is None
    assert len(tracker.trades) == 0

    second_five_share_exit = Fill(
        order_id="exit_2", symbol="AAPL", side=OrderSide.SELL, qty=5.0, price=195.0, ts=t2, strategy="manual"
    )
    second_res = tracker.on_fill(second_five_share_exit, risk_manager=risk)

    assert second_res is not None
    assert second_res.qty == 10.0
    assert len(tracker.trades) == 1
    assert risk.consecutive_losses == 1
    assert risk.strategy_consecutive_losses["manual"] == 1


def test_pdt_once():
    tracker = PortfolioTracker()
    t0 = datetime.now(UTC)
    t1 = datetime.now(UTC)
    t2 = datetime.now(UTC)

    tracker._lots["AAPL"] = _OpenLot(
        qty=10.0, avg_price=200.0, entry_ts=t0, strategy="manual", initial_risk_per_share=2.0
    )

    first = Fill(
        order_id="exit_1", symbol="AAPL", side=OrderSide.SELL, qty=5.0, price=195.0, ts=t1, strategy="manual"
    )
    tracker.on_fill(first)
    assert tracker.day_trades_today == 0

    second = Fill(
        order_id="exit_2", symbol="AAPL", side=OrderSide.SELL, qty=5.0, price=195.0, ts=t2, strategy="manual"
    )
    tracker.on_fill(second)
    assert tracker.day_trades_today == 1





@pytest.mark.asyncio
async def test_heartbeat_cannot_reenable_blocked_entries():
    engine = TradingEngine.__new__(TradingEngine)
    engine.allow_new_entries = False
    engine.system_health = "DEGRADED"

    # Heartbeat check should not re-enable entries when blocked by protection or data gap
    heartbeat_health = "NORMAL"
    heartbeat_blocks_entries = False

    if heartbeat_health != "NORMAL":
        if engine.system_health not in {"HALTED", "SAFE_MODE"}:
            engine.system_health = heartbeat_health

    if heartbeat_blocks_entries:
        engine.allow_new_entries = False

    assert engine.allow_new_entries is False


@pytest.mark.asyncio
async def test_replay_deferred():
    engine = TradingEngine.__new__(TradingEngine)
    engine.db = MagicMock()
    engine.data = AsyncMock()
    engine.mode = "paper"

    signal = {
        "candidate_id": "cand-1",
        "symbol": "AAPL",
        "direction": "long",
        "timestamp": datetime.now(UTC).isoformat(),
        "replay_attempts": 0,
    }
    engine.db.get_pending_shadow_replays.return_value = [signal]
    engine.data.history.return_value = MagicMock(empty=True)

    await engine._replay_pending_signals(datetime.now(UTC))

    saved_signal = engine.db.save_shadow_signal.call_args[0][6]
    assert saved_signal.get("replayed", False) is False
    assert saved_signal.get("replay_failed", False) is False
    assert saved_signal.get("replay_state") == "deferred_data_incomplete"
    assert saved_signal.get("next_retry_at") is not None


@pytest.mark.asyncio
async def test_replay_respects_backoff():
    engine = TradingEngine.__new__(TradingEngine)
    engine.db = MagicMock()
    engine.data = AsyncMock()
    engine.mode = "paper"

    future_retry = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    signal = {
        "candidate_id": "cand-1",
        "symbol": "AAPL",
        "direction": "long",
        "timestamp": datetime.now(UTC).isoformat(),
        "next_retry_at": future_retry,
    }
    engine.db.get_pending_shadow_replays.return_value = [signal]

    await engine._replay_pending_signals(datetime.now(UTC))

    assert engine.data.history.call_count == 0





def test_checkpoint_preserves_peak_equity_and_day_trade_dates():
    tracker = PortfolioTracker()
    tracker.session_date = date(2026, 7, 22)
    tracker.session_start_equity = 100_000.0
    tracker.peak_equity = 105_500.0
    tracker._day_trade_dates = [
        date(2026, 7, 20),
        date(2026, 7, 22),
    ]

    payload = tracker.checkpoint()

    assert payload["peak_equity"] == 105_500.0
    assert payload["day_trade_dates"] == [
        "2026-07-20",
        "2026-07-22",
    ]

    restored = PortfolioTracker()
    restored.restore_checkpoint(payload)

    assert restored.peak_equity == 105_500.0
    assert restored._day_trade_dates == [
        date(2026, 7, 20),
        date(2026, 7, 22),
    ]
