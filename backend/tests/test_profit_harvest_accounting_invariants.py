from datetime import UTC, datetime

from app.brokers.base import Fill
from app.core.domain import OrderSide
from app.portfolio.tracker import PortfolioTracker


def _open_long(tracker: PortfolioTracker, qty: float = 10.0) -> None:
    tracker.on_fill(
        Fill(
            order_id="entry",
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=qty,
            price=100.0,
            ts=datetime(2026, 7, 29, 14, 30, tzinfo=UTC),
            strategy="momentum",
        ),
        initial_stop=98.0,
    )


def test_profit_lock_stop_does_not_redefine_initial_risk():
    tracker = PortfolioTracker()
    _open_long(tracker)
    lot = tracker.open_positions()["AAPL"]
    assert lot.initial_stop_loss == 98.0
    assert lot.initial_risk_per_share == 2.0
    assert lot.initial_risk_dollars == 20.0

    tracker.note_stop("AAPL", 103.0)

    lot = tracker.open_positions()["AAPL"]
    assert lot.stop_loss == 103.0
    assert lot.initial_stop_loss == 98.0
    assert lot.initial_risk_per_share == 2.0
    assert lot.initial_risk_dollars == 20.0


def test_checkpoint_roundtrip_preserves_original_risk_and_current_profit_stop():
    tracker = PortfolioTracker()
    _open_long(tracker)
    tracker.note_stop("AAPL", 103.0)

    restored = PortfolioTracker()
    restored.restore_checkpoint(tracker.checkpoint())
    lot = restored.open_positions()["AAPL"]

    assert lot.stop_loss == 103.0
    assert lot.initial_stop_loss == 98.0
    assert lot.initial_risk_per_share == 2.0
    assert lot.initial_risk_dollars == 20.0


def test_realized_r_uses_original_risk_after_profit_stop_ratchet_and_restart():
    tracker = PortfolioTracker()
    _open_long(tracker)
    tracker.note_stop("AAPL", 103.0)

    restored = PortfolioTracker()
    restored.restore_checkpoint(tracker.checkpoint())
    trade = restored.on_fill(
        Fill(
            order_id="exit",
            symbol="AAPL",
            side=OrderSide.SELL,
            qty=10.0,
            price=104.0,
            ts=datetime(2026, 7, 29, 15, 0, tzinfo=UTC),
            strategy="momentum",
            reason="profit_lock",
        ),
        peak_price=105.0,
        trough_price=99.0,
    )

    assert trade is not None
    assert trade.initial_stop == 98.0
    assert trade.initial_risk == 2.0
    assert trade.r_multiple == 2.0
    assert trade.mfe == 5.0
    assert trade.capture_efficiency == 0.8
