"""Unit tests for volume-constrained fills and warmup bar support in production replay."""
import pytest
from datetime import datetime, timezone
from app.replay.broker import ReplayBroker
from app.replay.ledger import ReplayLedger
from app.core.domain import OrderRequest, OrderSide, OrderType, OrderStatus

@pytest.mark.asyncio
async def test_volume_constrained_partial_fill():
    """Verify that ReplayBroker caps fill quantity to max_volume_pct when volume_constrained is True."""
    broker = ReplayBroker(
        starting_cash=100_000.0,
        fill_model="ohlc_touch",
        volume_constrained=True,
        max_volume_pct=0.10, # Max 10% of bar volume per tick
    )
    broker.set_time(datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc))

    # Submit a limit order to buy 1,000 shares at $100
    req = OrderRequest(
        symbol="AAPL",
        qty=1000.0,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        limit_price=100.0,
        client_order_id="test_vol_ord_1",
    )
    order = await broker.submit(req)
    assert order.status == OrderStatus.SUBMITTED

    # Process bar with touch price (low=99.5) and volume of 2,000 shares
    # 10% of 2,000 = 200 shares max fill
    broker.process_ohlc(
        symbol="AAPL",
        open_px=100.0,
        high_px=101.0,
        low_px=99.5,
        close_px=100.5,
        ts=datetime(2026, 7, 20, 14, 31, tzinfo=timezone.utc),
        volume=2000.0,
    )

    fills = broker.drain_fills()
    assert len(fills) == 1
    assert fills[0].qty == 200.0
    assert order.filled_qty == 200.0
    assert order.remaining_qty == 800.0
    assert order.status == OrderStatus.PARTIALLY_FILLED


def test_replay_ledger_production_fill_recording():
    """Verify ReplayLedger records production fills for forensic comparison."""
    ledger = ReplayLedger("test_rpl_1")
    ts = datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc)
    
    ledger.record_production_fill(
        timestamp=ts,
        symbol="AAPL",
        fill_details={"price": 150.25, "qty": 100, "venue": "NASDAQ"}
    )
    
    assert len(ledger.production_fills) == 1
    assert ledger.production_fills[0]["symbol"] == "AAPL"
    assert ledger.production_fills[0]["details"]["price"] == 150.25
    payload = ledger.to_payload()
    assert "production_fills" in payload
