"""Unit tests for Sub-$5 Penny Stock Quantitative Guardrails.

Verifies:
1. Volatility Noise Floor Stop Cushioning (prevents micro-stop tick leverage blowouts).
2. Sub-$5 Dollar Risk Capping with 15% Execution Slippage Buffer.
3. Sub-$5 Notional Exposure Cap (10% equity ceiling).
4. Share Quantity Ceilings.
5. SignalPipeline Warmup Reduced rejection of un-modeled sub-$5 penny setups.
"""
from __future__ import annotations

import pytest

from app.config import RiskConfig
from app.core.domain import Regime, Signal, SignalAction
from app.indicators.ta import enrich
from app.risk.manager import AccountContext, RiskManager
from tests.conftest import MID_SESSION, make_bars


def penny_ctx(equity=100_000.0) -> AccountContext:
    return AccountContext(
        equity=equity,
        cash=equity,
        positions=[],
        day_pnl=0.0,
        session_start_equity=equity,
        peak_equity=equity,
        day_trades_today=0,
        rolling_day_trades=0,
        is_paper=True,
    )


def test_penny_stop_cushioning_and_share_ceiling(open_clock):
    cfg = RiskConfig(
        min_price=0.50,
        risk_per_trade_pct=0.25,
        min_stop_floor_atr_mult=0.60,
        sub5_max_notional_pct=10.0,
        sub5_max_risk_dollars=250.0,
        sub5_slippage_buffer=0.85,
        max_order_shares=5000,
    )
    rm = RiskManager(cfg, open_clock)

    # Simulate HTZ at $2.22 with ATR=0.19 and a naive 3-cent stop ($2.19)
    price = 2.22
    raw_stop = 2.19  # 3-cent stop
    atr = 0.19

    bars = enrich(make_bars(120, price=price))
    bars.loc[bars.index[-1], "atr14"] = atr
    bars.loc[bars.index[-1], "close"] = price

    sig = Signal(
        symbol="HTZ",
        action=SignalAction.ENTER_LONG,
        confidence=0.75,
        strategy="momentum",
        reason="unit test",
        price=price,
        stop_loss=raw_stop,
    )

    decision = rm.evaluate_entry(sig, penny_ctx(equity=100_000.0), bars, now=MID_SESSION)
    assert decision.approved, f"Expected approval, got: {decision.reason}"
    assert decision.order is not None

    # 1. Verify Stop was cushioned: min_noise = max(0.60 * 0.19, 2.22 * 0.005, 0.01) = 0.114
    effective_stop = decision.order.stop_loss
    assert effective_stop <= 2.11
    assert abs(price - effective_stop) >= 0.11  # cushioned from 0.03 to 0.11

    # 2. Verify Share Quantity is strictly capped at max_order_shares (5,000)
    assert decision.order.qty <= 5000

    # 3. Verify Notional exposure is capped at 10% equity ($10,000)
    notional = decision.order.qty * price
    assert notional <= 10_000.0

    # 4. Verify Dollar Risk is capped to sub5_max_risk_dollars * slippage_buffer (250 * 0.85 = 212.50)
    stop_dist = abs(price - effective_stop)
    calculated_risk = decision.order.qty * stop_dist
    assert calculated_risk <= (250.0 * 0.85) + 5.0


def test_sub2_penny_stock_share_ceiling(open_clock):
    cfg = RiskConfig(
        min_price=0.50,
        risk_per_trade_pct=0.25,
        min_stop_floor_atr_mult=0.60,
        sub5_max_notional_pct=10.0,
        sub5_max_risk_dollars=250.0,
        sub5_slippage_buffer=0.85,
        max_order_shares=5000,
    )
    rm = RiskManager(cfg, open_clock)

    price = 1.50
    raw_stop = 1.48
    atr = 0.10

    bars = enrich(make_bars(120, price=price))
    bars.loc[bars.index[-1], "atr14"] = atr
    bars.loc[bars.index[-1], "close"] = price

    sig = Signal(
        symbol="NCPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.75,
        strategy="momentum",
        reason="unit test",
        price=price,
        stop_loss=raw_stop,
    )

    decision = rm.evaluate_entry(sig, penny_ctx(equity=100_000.0), bars, now=MID_SESSION)
    assert decision.approved
    assert decision.order is not None
    # For price < 2.0, max shares ceiling is max(5000, 10000) = 10000
    assert decision.order.qty <= 10000
    # And notional <= $10,000
    assert decision.order.qty * price <= 10_000.0


def test_warmup_reduced_blocks_unmodeled_penny_stock():
    from unittest.mock import MagicMock
    from app.strategies.pipeline import SignalPipeline
    from app.ai.prediction import MetaPrediction

    mock_model = MagicMock()
    mock_model.score_action.return_value = 0.65
    mock_model.score_action_detailed.return_value = MagicMock(
        probability=0.65, model_source="SPECIALIST", specialist_status="ACTIVE",
        specialist_auc=0.62, specialist_min_auc=0.53, fallback_reason=None
    )

    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.48)
    pipeline.trade_score_engine.risk_config.enable_opening_blackout = False

    # Return NO_MODEL bypass
    pipeline.meta_label_engine.predict = MagicMock(return_value=MetaPrediction(
        strategy="test",
        prob_profitable=0.50,
        approved=True,
        threshold=0.48,
        n_training_samples=0,
        decision="BYPASS",
        source="NO_MODEL",
        reason="No trained meta-label artifact found for test",
    ))

    # Signal for sub-$5 penny stock
    sig = Signal(
        symbol="PENY",
        action=SignalAction.ENTER_LONG,
        confidence=0.70,
        strategy="test",
        reason="unit test",
        price=2.50,
        stop_loss=2.40,
        take_profit=2.80,
    )

    # Short dataframe with 10 bars (triggers is_warmup_reduced)
    df = make_bars(10, price=2.50)

    accepted, rejected = pipeline.process(
        symbol="PENY",
        df=df,
        signals=[sig],
        regime=Regime.RANGE,
    )
    assert len(accepted) == 0
    assert len(rejected) == 1
    assert "Warmup reduced path disallows unverified penny stocks without trained meta-model" in rejected[0][1]
