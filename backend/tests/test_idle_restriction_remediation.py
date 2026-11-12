import pytest
from unittest.mock import MagicMock
import datetime
from datetime import date, datetime as dt, timezone
import pandas as pd

from app.config import RiskConfig
from app.risk.manager import RiskManager, AccountContext
from app.core.domain import Signal, SignalAction
from app.ai.monte_carlo_gate import MonteCarloGate
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.base import StrategyContext
from app.core.domain import Regime


def test_stale_multi_day_loss_counts_not_restored_across_sessions():
    """Verify that daily loss counts from a prior day (e.g. AVGO 2/2) do not lockout today."""
    mock_clock = MagicMock()
    mock_clock.session_date.return_value = date(2026, 11, 12)
    mock_clock.is_open.return_value = True

    rc = RiskConfig(symbol_max_daily_losses=2)
    rm = RiskManager(rc, mock_clock)

    # Checkpoint from yesterday (2026-11-11) with historical 2-loss lockout on AVGO and AMD
    stale_checkpoint_data = {
        "session_date": "2026-11-11",
        "symbol_loss_counts": {"AVGO": 2, "AMD": 2},
        "symbol_trade_counts": {"AVGO": 2, "AMD": 2},
        "consecutive_losses": 4,
    }

    # Restoring on today's session (2026-11-12)
    rm.restore_risk_state(stale_checkpoint_data, current_session_date=date(2026, 11, 12))

    # Daily loss counts and trade counts must be reset for today's new session!
    assert rm.symbol_loss_counts.get("AVGO", 0) == 0
    assert rm.symbol_loss_counts.get("AMD", 0) == 0
    assert rm.symbol_trade_counts.get("AVGO", 0) == 0

    # Today's session data must be preserved if restored within the same session
    today_checkpoint_data = {
        "session_date": "2026-11-12",
        "symbol_loss_counts": {"AMZN": 1},
        "symbol_trade_counts": {"AMZN": 1},
    }
    rm.restore_risk_state(today_checkpoint_data, current_session_date=date(2026, 11, 12))
    assert rm.symbol_loss_counts.get("AMZN") == 1
    assert rm.symbol_trade_counts.get("AMZN") == 1


def test_index_etf_vs_single_stock_target_expansion():
    """Verify single stocks enforce full >= 3.5% target while index ETFs are calibrated by ATR."""
    mock_clock = MagicMock()
    mock_clock.is_open.return_value = True

    rc = RiskConfig(min_profit_target_pct=3.5, enable_native_bracket_targets=True)
    rm = RiskManager(rc, mock_clock)

    act_ctx = AccountContext(
        equity=100000.0,
        cash=100000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=100000.0,
        peak_equity=100000.0,
        day_trades_today=0,
        rolling_day_trades=0,
        is_paper=True,
    )

    bars_df = pd.DataFrame({
        "open": [100.0] * 10,
        "high": [100.5] * 10,
        "low": [99.5] * 10,
        "close": [100.0] * 10,
        "volume": [10000.0] * 10,
        "atr14": [0.50] * 10,
    })

    # Single stock (AMD) with tiny initial target (+1.0%)
    sig_stock = Signal(
        symbol="AMD",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="momentum",
        reason="test",
        price=100.0,
        stop_loss=99.5,
        take_profit=101.0,
    )
    dec_stock = rm.evaluate_entry(sig_stock, act_ctx, bars_df)
    assert dec_stock.approved
    # Must enforce full 3.5% floor for single stocks
    assert dec_stock.order.take_profit >= 103.50

    # Index ETF (SPY at $750, ATR = $3.75 -> ATR% = 0.5%)
    spy_bars = pd.DataFrame({
        "open": [750.0] * 10,
        "high": [752.0] * 10,
        "low": [748.0] * 10,
        "close": [750.0] * 10,
        "volume": [50000.0] * 10,
        "atr14": [3.75] * 10,
    })
    sig_spy = Signal(
        symbol="SPY",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="orb",
        reason="test",
        price=750.0,
        stop_loss=747.5,
        take_profit=755.0,
    )
    dec_spy = rm.evaluate_entry(sig_spy, act_ctx, spy_bars)
    assert dec_spy.approved
    # SPY target must NOT be forced to impossible 103.5% ($776.25); calibrated to realistic ATR
    assert dec_spy.order.take_profit < 765.0
    assert dec_spy.order.take_profit >= 755.0


def test_monte_carlo_permits_high_r_asymmetric_setups():
    """Verify Monte Carlo gate does not veto high-R asymmetric trades with positive edge."""
    gate = MonteCarloGate(min_win_prob=0.35)
    # High-R setup: r_target = 4.0R (breakeven is 20%)
    # Even if win prob is 22% (below flat 35% and below old 25% clamp), it has positive edge!
    stats = {
        "p_target": 0.22,
        "p_stop": 0.65,
        "p_neither": 0.13,
        "avg_r": 0.23,
        "r_target": 4.0,
        "predicted_peak_r": 2.5,
    }
    gate.extract_ratio_pool = MagicMock(return_value=(None, None, None))
    gate.simulate = MagicMock(return_value=stats)
    sig = Signal(symbol="TEST", action=SignalAction.ENTER_LONG, confidence=0.7, strategy="orb", reason="test", price=100.0, stop_loss=99.0, take_profit=104.0)
    approved, res_stats, reason = gate.evaluate_signal(sig, pd.DataFrame({"close": [100.0]}))
    assert approved
    assert "Monte Carlo approved" in reason
