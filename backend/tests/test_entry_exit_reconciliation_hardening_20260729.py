"""Regression coverage for the entry -> reconciliation -> protected-position boundary.

The production source is materialized by:
    python tools/apply_entry_exit_reconciliation_hardening_20260729.py
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.domain import Order, OrderSide, OrderStatus, OrderType
from app.core.engine import TradingEngine


def _engine(cid="cid-1", submitted_at=None):
    e = object.__new__(TradingEngine)
    e.mode = "live"
    e.pending_reconciliation_orders = {
        cid: {
            "symbol": "AAPL",
            "client_order_id": cid,
            "side": "buy",
            "qty": 10.0,
            "order_type": "limit",
            "limit_price": 100.0,
            "submitted_at": (submitted_at or datetime.now(UTC)).isoformat(),
        }
    }
    e.db = MagicMock()
    e.broker = SimpleNamespace(get_order_by_client_id=AsyncMock(return_value=None))
    return e


@pytest.mark.asyncio
async def test_single_not_found_never_releases_ambiguous_entry_guard():
    now = datetime.now(UTC)
    e = _engine(submitted_at=now - timedelta(minutes=5))
    await e._reconcile_pending_submissions(now=now)
    assert "cid-1" in e.pending_reconciliation_orders
    assert e.pending_reconciliation_orders["cid-1"]["reconcile_not_found_count"] == 1
    e.db.save_order.assert_not_called()


@pytest.mark.asyncio
async def test_query_timeout_never_counts_as_absence_or_releases_guard():
    now = datetime.now(UTC)
    e = _engine(submitted_at=now - timedelta(minutes=5))
    e.broker.get_order_by_client_id.side_effect = TimeoutError("broker lookup unavailable")
    await e._reconcile_pending_submissions(now=now)
    assert "cid-1" in e.pending_reconciliation_orders
    assert "reconcile_not_found_count" not in e.pending_reconciliation_orders["cid-1"]
    e.db.save_order.assert_not_called()


@pytest.mark.asyncio
async def test_delayed_broker_visibility_resolves_positive_truth_without_duplicate_release():
    now = datetime.now(UTC)
    e = _engine(submitted_at=now - timedelta(minutes=5))
    broker_order = Order(
        id="broker-1", client_order_id="cid-1", symbol="AAPL", side=OrderSide.BUY,
        qty=10.0, order_type=OrderType.LIMIT, limit_price=100.0,
        status=OrderStatus.SUBMITTED,
    )
    e.broker.get_order_by_client_id.side_effect = [None, None, broker_order]

    await e._reconcile_pending_submissions(now=now)
    assert "cid-1" in e.pending_reconciliation_orders
    await e._reconcile_pending_submissions(now=now + timedelta(seconds=1))
    assert "cid-1" in e.pending_reconciliation_orders
    await e._reconcile_pending_submissions(now=now + timedelta(seconds=2))

    assert "cid-1" not in e.pending_reconciliation_orders
    e.db.save_order.assert_called_once_with(broker_order, "live")


@pytest.mark.asyncio
async def test_repeated_old_not_found_evidence_eventually_resolves_absent():
    now = datetime.now(UTC)
    e = _engine(submitted_at=now - timedelta(minutes=5))
    for offset in (0, 1):
        await e._reconcile_pending_submissions(now=now + timedelta(seconds=offset))
        assert "cid-1" in e.pending_reconciliation_orders

    await e._reconcile_pending_submissions(now=now + timedelta(seconds=2))
    assert "cid-1" not in e.pending_reconciliation_orders
    rejected = e.db.save_order.call_args.args[0]
    assert rejected.status == OrderStatus.REJECTED
    assert "repeated broker not-found" in rejected.reason


@pytest.mark.asyncio
async def test_young_submission_remains_guarded_even_after_three_not_found_observations():
    now = datetime.now(UTC)
    e = _engine(submitted_at=now - timedelta(seconds=10))
    for offset in (0, 1, 2, 3):
        await e._reconcile_pending_submissions(now=now + timedelta(seconds=offset))
    assert "cid-1" in e.pending_reconciliation_orders
    e.db.save_order.assert_not_called()


def test_tick_reconciles_ambiguous_submissions_before_heartbeat():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    tick = src[src.index("    async def _tick(self)"):src.index("        # Run pre-market scanner", src.index("    async def _tick(self)"))]
    assert tick.index("await self._reconcile_pending_submissions(now=now)") < tick.index("await self._check_system_heartbeat(now)")
    assert tick.count("_reconcile_pending_submissions") == 1


def test_heartbeat_never_deletes_pending_reconciliation_guard():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    hb = src[src.index("    async def _check_system_heartbeat"):]
    hb = hb[:hb.index("    async def", 20)] if "    async def" in hb[20:] else hb
    assert "pending_reconciliation_orders.pop" not in hb
    assert "order_bad = bool(self.pending_reconciliation_orders)" in hb


def test_reconciliation_safe_mode_has_final_severity_floor():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    hb = src[src.index("    async def _check_system_heartbeat"):]
    assert 'elif getattr(self, "reconciliation_mismatch", False):' in hb
    assert 'self.system_health = "SAFE_MODE"' in hb
    assert 'self.allow_new_entries = False' in hb
