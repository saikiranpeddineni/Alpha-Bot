from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.domain import OrderRequest, OrderSide, OrderType
from app.replay.broker import ReplayBroker


@pytest.mark.asyncio
async def test_replay_commission_is_deducted_from_cash_and_equity():
    broker = ReplayBroker(
        starting_cash=10_000.0,
        commission_per_share=0.01,
        minimum_commission=1.0,
    )
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    broker.process_ohlc("AAPL", 100.0, 100.0, 100.0, 100.0, now)
    await broker.submit(OrderRequest(
        symbol="AAPL", side=OrderSide.BUY, qty=100,
        order_type=OrderType.MARKET, strategy="test",
    ))
    account = await broker.account()
    assert account.cash == pytest.approx(0.0 + 10_000.0 - 10_000.0 - 1.0)
    assert account.equity == pytest.approx(9_999.0)
