from datetime import UTC, datetime, timedelta

from app.core.domain import Order, OrderSide, OrderStatus, OrderType, Position
from app.db.database import Database


def _order(symbol, strategy, *, minutes, leg="entry", side=OrderSide.BUY):
    return Order(
        id=f"{symbol}-{strategy}-{minutes}",
        symbol=symbol,
        side=side,
        qty=10,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        filled_qty=10,
        strategy=strategy,
        leg=leg,
        submitted_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )


def test_latest_strategy_ignores_newer_emergency_protection(tmp_path):
    db = Database(tmp_path / "strategy.db")
    db.save_order(_order("SQQQ", "orb", minutes=0), "paper")
    db.save_order(_order("SQQQ", "emergency_protection", minutes=5, leg="stop", side=OrderSide.SELL), "paper")

    assert db.get_latest_order_strategy("SQQQ", "paper") == "orb"
    assert db.get_latest_entry_order_payload("SQQQ", "paper")["strategy"] == "orb"


def test_latest_true_entry_wins_when_multiple_entries_exist(tmp_path):
    db = Database(tmp_path / "strategy.db")
    db.save_order(_order("PLTR", "rsi2_reversion", minutes=0), "paper")
    db.save_order(_order("PLTR", "orb", minutes=10), "paper")
    db.save_order(_order("PLTR", "emergency_protection", minutes=15, leg="stop", side=OrderSide.SELL), "paper")

    assert db.get_latest_order_strategy("PLTR", "paper") == "orb"


def test_position_strategy_field_is_mutable_for_reconciliation():
    p = Position(symbol="NOK", qty=-5, avg_entry_price=9.23, market_price=9.01, strategy="emergency_protection")
    p.strategy = "momentum"
    assert p.strategy == "momentum"
