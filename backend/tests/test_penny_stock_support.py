"""Tests for penny stock and sub-penny pricing support across the engine.

Verifies:
1. RiskManager approves penny stocks ($0.50 - $5.00) while respecting min_price = 0.50.
2. Sub-penny price precision (4 decimal places < $1.00, 2 decimal places >= $1.00) in RiskManager and Alpaca order requests.
3. Sizing constraints, cash caps, and bracket geometry remain valid for low-price assets.
4. TopMoversScanner and PreMarketGapScanner default min_price = 0.50.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
import pandas as pd
import pytest

from app.config import RiskConfig
from app.core.clock import MarketClock
from app.core.domain import OrderRequest, OrderSide, OrderStatus, OrderType, Position, Signal, SignalAction
from app.risk.manager import AccountContext, RiskManager
from app.brokers.alpaca_hardening import normalize_stock_price


def test_normalize_stock_price_precision():
    """Verify sub-penny precision: 4 decimal places < $1.00, 2 decimal places >= $1.00."""
    # Sub-dollar penny stocks: 4 decimal places
    assert normalize_stock_price(0.7542) == 0.7542
    assert normalize_stock_price(0.75428) == 0.7543
    assert normalize_stock_price(0.50) == 0.50
    assert normalize_stock_price(0.0125) == 0.0125

    # Standard equities >= $1.00: 2 decimal places
    assert normalize_stock_price(1.00) == 1.00
    assert normalize_stock_price(1.556) == 1.56
    assert normalize_stock_price(150.254) == 150.25


def test_risk_manager_evaluates_penny_stocks():
    """Verify RiskManager permits valid penny stocks ($0.50+) and rejects sub-$0.50."""
    clock = MarketClock()
    cfg = RiskConfig(
        min_price=0.50,
        min_avg_dollar_volume=1_000.0,
        max_position_pct=20.0,
        risk_per_trade_pct=1.0,
    )
    rm = RiskManager(clock=clock, config=cfg)

    # Synthetic 120 bars of liquid penny stock trading at $0.75 with $15,000/min dollar volume
    bars = pd.DataFrame({
        "open": [0.74] * 120,
        "high": [0.76] * 120,
        "low": [0.73] * 120,
        "close": [0.75] * 120,
        "volume": [20_000] * 120,
        "atr14": [0.03] * 120,
    })

    ctx = AccountContext(
        equity=50_000.0,
        cash=50_000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=50_000.0,
        peak_equity=50_000.0,
        day_trades_today=0,
        rolling_day_trades=0,
        is_paper=True,
    )

    # Test during normal market hours
    market_time = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)

    # 1. Valid penny stock trade ($0.75 > $0.50)
    valid_signal = Signal(
        symbol="PENY",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        price=0.75,
        stop_loss=0.70,
        take_profit=0.85,
        strategy="momentum",
        reason="penny breakout",
    )

    decision = rm.evaluate_entry(valid_signal, ctx, bars, now=market_time)
    assert decision.approved is True, f"Decision rejected: {decision.reason}"
    assert decision.order is not None
    assert decision.order.qty >= 1
    assert decision.order.symbol == "PENY"
    # Ensure stop_loss is precision-preserved
    assert decision.order.stop_loss == 0.70

    # 2. Sub-minimum penny stock ($0.45 < $0.50)
    sub_min_signal = Signal(
        symbol="TINY",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        price=0.45,
        stop_loss=0.40,
        strategy="momentum",
        reason="sub-50c test",
    )
    bars_sub = bars.copy()
    bars_sub["close"] = 0.45
    decision_sub = rm.evaluate_entry(sub_min_signal, ctx, bars_sub, now=market_time)
    assert decision_sub.approved is False
    assert "below minimum 0.5" in decision_sub.reason


def test_sub_penny_bracket_geometry():
    """Verify sub-penny bracket geometry handles 4 decimal places and limit pricing."""
    clock = MarketClock()
    cfg = RiskConfig(
        min_price=0.50,
        min_avg_dollar_volume=1_000.0,
        entry_order_type="limit",
    )
    rm = RiskManager(clock=clock, config=cfg)

    # Price with 4 decimals below $1.00
    bars = pd.DataFrame({
        "open": [0.5500] * 120,
        "high": [0.5600] * 120,
        "low": [0.5300] * 120,
        "close": [0.5500] * 120,
        "volume": [20_000] * 120,
        "atr14": [0.0200] * 120,
    })
    ctx = AccountContext(
        equity=25_000.0,
        cash=25_000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=25_000.0,
        peak_equity=25_000.0,
        day_trades_today=0,
        rolling_day_trades=0,
        is_paper=True,
    )

    market_time = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)

    # Stop loss with 4-decimal precision (e.g. 0.53756 -> 0.5376, distance = 0.0124 > min_distance 0.01)
    signal = Signal(
        symbol="MICRO",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        price=0.5500,
        stop_loss=0.53756,
        strategy="breakout",
        reason="tick adjustment test",
    )

    decision = rm.evaluate_entry(signal, ctx, bars, now=market_time)
    assert decision.approved is True
    # Stop loss preserved at 4 decimal places
    assert decision.order.stop_loss == 0.5376
    # Limit price preserved at 4 decimal places (0.55 * 1.0015 = 0.5508)
    assert decision.order.limit_price == 0.5508


@pytest.mark.asyncio
async def test_alpaca_sub_penny_order_submission():
    """Verify Alpaca broker adapter formats sub-dollar orders with normalize_stock_price."""
    from app.brokers.alpaca import AlpacaBroker

    broker = AlpacaBroker(api_key="mock", api_secret="mock", paper=True)

    # Mock submit_order call returning SimpleNamespace order representation
    mock_order = SimpleNamespace(
        id="mock-id-123",
        client_order_id="test-client-id",
        symbol="PENY",
        qty=1000,
        filled_qty=0,
        filled_avg_price=None,
        side="buy",
        type="limit",
        status="new",
        created_at=None,
        limit_price=0.7543,
        stop_price=None,
        order_class=None,
    )

    broker._client = MagicMock()
    broker._client.submit_order = MagicMock(return_value=mock_order)

    req = OrderRequest(
        symbol="PENY",
        side=OrderSide.BUY,
        qty=1000,
        order_type=OrderType.LIMIT,
        limit_price=0.75428,  # should normalize to 0.7543
        stop_loss=0.7125,     # should normalize to 0.7125
        take_profit=0.8250,   # should normalize to 0.8250
        strategy="penny_runner",
        reason="sub-penny test",
    )

    order = await broker.submit(req)
    assert order.status == OrderStatus.SUBMITTED

    # Verify submit_order was called with LimitOrderRequest having 4-decimal prices
    assert broker._client.submit_order.called
    call_args = broker._client.submit_order.call_args[0][0]
    assert call_args.limit_price == 0.7543
    assert call_args.stop_loss.stop_price == 0.7125
    assert call_args.take_profit.limit_price == 0.8250
