"""Behavioral regression for the 2026-08-24 partial-close latch wedge.

Production sequence that day (CRWV 15:44 UTC, then V/AMZN):

    1. PIS FLOOR_TAG_RUNNER partial close submitted via _close_position_guarded
       -> latch armed, tracked order never registered locally.
    2. Partial fill drains. Residual runner lot remains, so the
       lot-gone fallback could not release the latch.
    3. Every subsequent PROTECTION_BREACH exit was swallowed by
       "skipping duplicate close" for 30-80 minutes; runners rode naked to EOD.

These tests drive _drain_fills directly with a paper-style broker stub.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.brokers.base import Fill
from app.core.domain import OrderSide, OrderStatus


def _make_engine(tmp_path):
    from app.core.engine import TradingEngine
    from app.config import Settings
    from app.db.database import Database

    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test.db"),
        DEFAULT_WATCHLIST="SPY",
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)
    return engine, db


@pytest.mark.anyio
async def test_partial_close_fill_releases_latch_by_fill_truth(tmp_path):
    """A fully-executed partial close must release the guard even when the
    tracked close order cannot be resolved into an Order object and a residual
    lot remains."""
    engine, db = _make_engine(tmp_path)
    try:
        # Broker returns an order object that was never registered locally.
        from app.core.domain import Order as OrderModel, OrderType

        close_order = OrderModel(
            id="close-1",
            symbol="CRWV",
            side=OrderSide.SELL,
            qty=107.0,
            order_type=OrderType.MARKET,
            status=OrderStatus.SUBMITTED,
        )
        engine.broker = AsyncMock()
        engine.broker.close_position = AsyncMock(return_value=close_order)
        engine.broker.drain_fills = AsyncMock(return_value=[])

        await engine._close_position_guarded("CRWV", reason="FLOOR_TAG_RUNNER", qty=107.0)
        assert "CRWV" in engine._pending_close_symbols

        # Simulate the partial close fill arriving WITHOUT any resolvable order:
        # portfolio still holds the 108-share runner remainder.
        now = datetime.now(UTC)
        fill = Fill(
            order_id="close-1",
            symbol="CRWV",
            side=OrderSide.SELL,
            qty=107.0,
            price=85.5,
            ts=now,
            cumulative_qty=107.0,
            strategy="bb_squeeze",
            reason="PIS Exit Reason: FLOOR_TAG_RUNNER",
        )
        runner_lot = SimpleNamespace(qty=108.0, stop_loss=84.6)
        engine.portfolio = SimpleNamespace(
            _lots={"CRWV": runner_lot},
            on_fill=MagicMock(return_value=None),
            note_stop=MagicMock(),
        )
        engine.risk = SimpleNamespace(get_risk_state=lambda: None)

        def fake_drain():
            return [fill]

        engine.broker.drain_fills = fake_drain
        engine._account_cache_ttl = 0
        await engine._drain_fills(now)

        assert "CRWV" not in engine._pending_close_symbols, (
            "latch must release from fill truth: cumulative_qty >= requested close size"
        )
    finally:
        db.close()


@pytest.mark.anyio
async def test_stale_latch_is_force_released_so_pis_can_retry(tmp_path):
    """A latch whose close never confirmed must expire instead of blocking
    every future PIS exit for the session."""
    engine, db = _make_engine(tmp_path)
    try:
        engine._pending_close_symbols.add("V")
        engine._get_pending_close_order_ids()["V"] = "never-resolves"
        engine._pending_close_qtys["V"] = 24.0
        engine._pending_close_submitted_at["V"] = datetime.now(UTC) - timedelta(seconds=300)

        await engine._reconcile_pending_closes(datetime.now(UTC))

        assert "V" not in engine._pending_close_symbols
    finally:
        db.close()


@pytest.mark.anyio
async def test_fresh_latch_survives_reconciliation(tmp_path):
    """An in-flight close younger than the staleness deadline keeps its guard."""
    engine, db = _make_engine(tmp_path)
    try:
        engine._pending_close_symbols.add("NVDA")
        engine._get_pending_close_order_ids()["NVDA"] = "in-flight-1"
        engine._pending_close_qtys["NVDA"] = 50.0
        engine._pending_close_submitted_at["NVDA"] = datetime.now(UTC) - timedelta(seconds=5)

        await engine._reconcile_pending_closes(datetime.now(UTC))

        assert "NVDA" in engine._pending_close_symbols
    finally:
        db.close()
