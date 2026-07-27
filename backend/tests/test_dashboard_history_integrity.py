from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.api.dashboard_history import durable_orders, durable_trades
from app.db.database import Database, OrderRow, TradeRow


def _ts(minutes: int) -> datetime:
    return datetime(2026, 7, 27, 14, 30, tzinfo=UTC) + timedelta(minutes=minutes)


def test_orders_are_loaded_from_durable_ledger_not_runtime_memory(tmp_path):
    db = Database(tmp_path / "dashboard.db")
    with db._session() as session:
        session.add_all([
            OrderRow(id="paper-old", ts=_ts(1), mode="paper", payload=json.dumps({"id": "paper-old", "submitted_at": _ts(1).isoformat()})),
            OrderRow(id="paper-new", ts=_ts(2), mode="paper", payload=json.dumps({"id": "paper-new", "submitted_at": _ts(2).isoformat()})),
            OrderRow(id="live-other", ts=_ts(3), mode="live", payload=json.dumps({"id": "live-other", "submitted_at": _ts(3).isoformat()})),
        ])
        session.commit()

    rows = durable_orders(db, "paper", limit=200)

    assert [row["id"] for row in rows] == ["paper-new", "paper-old"]


def test_trades_use_database_as_authoritative_history_when_checkpoint_is_shorter(tmp_path):
    db = Database(tmp_path / "dashboard.db")
    with db._session() as session:
        for i in range(50):
            payload = {
                "id": f"trade-{i:02d}",
                "symbol": "SPY",
                "side": "long",
                "qty": 1,
                "entry_price": 100.0,
                "exit_price": 101.0,
                "entry_at": _ts(i).isoformat(),
                "exit_at": _ts(i + 1).isoformat(),
                "pnl": 1.0,
                "pnl_pct": 1.0,
                "strategy": "test",
                "exit_reason": "test",
            }
            session.add(TradeRow(
                id=payload["id"],
                exit_at=_ts(i + 1),
                symbol="SPY",
                pnl=1.0,
                mode="paper",
                payload=json.dumps(payload),
            ))
        session.commit()

    # A runtime checkpoint containing only 22 trades must not truncate the UI.
    rows = durable_trades(db, "paper", limit=2000)

    assert len(rows) == 50
    assert rows[0]["id"] == "trade-49"
    assert rows[-1]["id"] == "trade-00"
