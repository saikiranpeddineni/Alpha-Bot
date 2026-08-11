from __future__ import annotations

import asyncio

from app.brokers.profit_execution import install_profit_execution_hooks
from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType
from app.profit_dynamics import adaptive_ml_threshold


def test_context_adaptation_requires_configured_sample_floor():
    base = 0.55
    weak = adaptive_ml_threshold(
        base,
        {"trades_count": 1, "shrunk_expectancy": 5.0},
    )
    assert weak == base


def test_context_adaptation_moves_with_sufficient_positive_evidence():
    base = 0.55
    adapted = adaptive_ml_threshold(
        base,
        {"trades_count": 1000, "shrunk_expectancy": 0.8},
    )
    assert adapted < base
    assert 0.0 <= adapted <= 1.0


def test_target_metadata_is_not_sent_to_broker_but_is_preserved_on_order():
    class FakeBroker:
        async def submit(self, req):
            assert req.take_profit is None
            return Order(
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                stop_price=req.stop_price,
                stop_loss=req.stop_loss,
                status=OrderStatus.SUBMITTED,
            )

    broker = FakeBroker()
    install_profit_execution_hooks(broker)
    req = OrderRequest(
        symbol="TEST",
        side=OrderSide.BUY,
        qty=10,
        order_type=OrderType.MARKET,
        stop_loss=95.0,
        take_profit=110.0,
    )

    order = asyncio.run(broker.submit(req))
    assert order.take_profit == 110.0
    assert req.take_profit == 110.0
