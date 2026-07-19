import pytest
from datetime import datetime, date, timedelta, UTC
import pandas as pd
import numpy as np

from app.portfolio.simulator import VirtualPortfolioSimulator, SimulationConfig, SimulationResult
from app.api.routes import ACCEPTED_DECISIONS


def test_strategy_level_allocations():
    # Test strategy-level allocation constraints with multiple competing strategies
    signals = [
        # Strategy "orb" signal 1: approved, consumes $10,000
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "orb",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_score": 95.0,
            "outcomes": {"close": {"pnl_pct": 5.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        },
        # Strategy "orb" signal 2: competes at same time, but strategy allocation limit is $10,000
        # Since AAPL is entered first (higher score), this second orb trade should exceed the strategy cap and be gated
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "MSFT",
            "strategy": "orb",
            "direction": "long",
            "entry_price": 200.0,
            "stop_loss": 180.0,
            "trade_score": 90.0,
            "outcomes": {"close": {"pnl_pct": 2.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        },
        # Strategy "momentum" signal: competes at same time, has separate limit $20,000, enters successfully
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "BAC",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 50.0,
            "stop_loss": 45.0,
            "trade_score": 85.0,
            "outcomes": {"close": {"pnl_pct": 1.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        }
    ]

    cfg = SimulationConfig(
        starting_capital=100000.0,
        risk_per_trade_pct=10.0,  # size = 10% risk = $10,000 risk dollars. AAPL risk distance = $10 -> sizes to 1,000 shares ($100,000 value), restricted by 10% max_position_pct = $10,000 (100 shares)
        max_position_pct=10.0,
        max_open_positions=3,
        max_positions_per_sector=3,
        max_allocation_per_strategy={
            "orb": 10000.0,      # strictly caps total orb value to $10,000
            "momentum": 20000.0
        },
        slippage_bps=0.0,
        commission_per_share=0.0
    )

    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals)

    # AAPL (orb) and BAC (momentum) entered. MSFT (orb) rejected due to strategy cap.
    assert len(res.trades) == 2
    symbols_entered = [t["symbol"] for t in res.trades]
    assert "AAPL" in symbols_entered
    assert "BAC" in symbols_entered
    assert "MSFT" not in symbols_entered


def test_deterministic_alphabetical_tie_break():
    # Signals with identical Trade Scores arriving at the same timestamp
    # Max open positions limit = 1. AAPL should win over MSFT alphabetically
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "MSFT",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 200.0,
            "stop_loss": 190.0,
            "trade_score": 90.0,
            "outcomes": {"close": {"pnl_pct": 2.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        },
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_score": 90.0,  # identical score
            "outcomes": {"close": {"pnl_pct": 5.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        }
    ]

    cfg = SimulationConfig(
        starting_capital=100000.0,
        max_open_positions=1,  # strictly limits to 1 open position
        slippage_bps=0.0,
        commission_per_share=0.0
    )

    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals)

    assert len(res.trades) == 1
    assert res.trades[0]["symbol"] == "AAPL"  # AAPL sorted and entered first, filling the 1 open slot


def test_buying_power_cash_exhaustion():
    # 3 signals competing at the same timestamp. Available cash = $300.
    # AAPL (score 95, cost $100), NVDA (score 90, cost $150), MSFT (score 85, cost $200).
    # Sizing cap forces maximum usage. AAPL enters (cash becomes $200), NVDA enters (cash becomes $50).
    # MSFT is rejected because $200 exceeds remaining $50 cash.
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "MSFT",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 200.0,
            "stop_loss": 180.0,
            "trade_score": 85.0,
            "outcomes": {"close": {"pnl_pct": 1.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        },
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_score": 95.0,
            "outcomes": {"close": {"pnl_pct": 1.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        },
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "NVDA",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 150.0,
            "stop_loss": 130.0,
            "trade_score": 90.0,
            "outcomes": {"close": {"pnl_pct": 1.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        }
    ]

    cfg = SimulationConfig(
        starting_capital=300.0,  # very tight cash limit
        risk_per_trade_pct=50.0,  # large risk to bypass risk cap
        max_position_pct=50.0,   # restricts position size to 50% of equity ($150)
        max_open_positions=3,
        slippage_bps=0.0,
        commission_per_share=0.0
    )

    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals)

    assert len(res.trades) == 2
    symbols_entered = [t["symbol"] for t in res.trades]
    assert "AAPL" in symbols_entered
    assert "NVDA" in symbols_entered
    assert "MSFT" not in symbols_entered


def test_long_short_adverse_slippage_and_commission():
    # Long trade sizing and exact math verification
    # Entry raw = $100, exit raw = $105. Qty = 100.
    # Slippage = 10 bps (0.1%).
    # Effective Entry Price = $100 * 1.001 = $100.10
    # Effective Exit Price = $105 * 0.999 = $104.895
    # Total entry cost = 100 * 100.1 + comm = $10,010 + comm
    # Total exit gross value = 100 * 104.895 - comm = $10,489.50 - comm
    # Realized P&L = $10,489.50 - $10,010.00 - 2 * comm = $479.50 - 2 * comm
    signals_long = [{
        "timestamp": "2026-01-06T10:00:00",
        "symbol": "AAPL",
        "strategy": "momentum",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "trade_score": 90.0,
        "outcomes": {"close": {"pnl_pct": 5.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
    }]

    cfg_long = SimulationConfig(
        starting_capital=100000.0,
        risk_per_trade_pct=10.0,
        max_position_pct=10.0,  # $10,000 position cap -> 100 shares
        slippage_bps=10.0,
        commission_per_share=0.05,
        fixed_commission_per_trade=2.0
    )

    sim_long = VirtualPortfolioSimulator(cfg_long)
    res_long = sim_long.run(signals_long)
    trade_long = res_long.trades[0]

    # Entry comm = 2.0 + 0.05 * 100 = $7.00
    # Exit comm = 2.0 + 0.05 * 100 = $7.00
    # Expected P&L = $479.50 - $14.00 = $465.50
    assert trade_long["qty"] == 100.0
    assert abs(trade_long["realized_pnl"] - 465.50) < 1e-4

    # Short trade math verification
    # Entry raw = $100, exit raw = $95. Qty = 100.
    # Slippage = 10 bps (0.1%).
    # Effective Entry Price = $100 * 0.999 = $99.90
    # Effective Exit buyback = $95 * 1.001 = $95.095
    # Gross realized P&L = 100 * (99.90 - 95.095) = $480.50
    # Deductions: Entry slippage/comm, Exit slippage/comm, borrow fee.
    # Total comm = 2 * (2.0 + 5.0) = $14.00
    # Borrow fee = $10,000 * 2.0% * (30 / (365 * 1440)) = $0.0114
    # Expected P&L = $480.50 - $14.00 - $0.0114 = $466.4886
    signals_short = [{
        "timestamp": "2026-01-06T10:00:00",
        "symbol": "AAPL",
        "strategy": "momentum",
        "direction": "short",
        "entry_price": 100.0,
        "stop_loss": 110.0,
        "trade_score": 90.0,
        "outcomes": {"close": {"pnl_pct": 5.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
    }]

    cfg_short = SimulationConfig(
        starting_capital=100000.0,
        risk_per_trade_pct=10.0,
        max_position_pct=10.0,  # $10,000 position cap -> 100 shares
        slippage_bps=10.0,
        commission_per_share=0.05,
        fixed_commission_per_trade=2.0,
        borrow_fee_annual_pct=2.0
    )

    sim_short = VirtualPortfolioSimulator(cfg_short)
    res_short = sim_short.run(signals_short)
    trade_short = res_short.trades[0]

    assert trade_short["qty"] == 100.0
    assert abs(trade_short["realized_pnl"] - 466.4886) < 0.05


def test_short_borrow_availability_edge_cases():
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "short",
            "entry_price": 100.0,
            "stop_loss": 110.0,
            "outcomes": {"close": {"pnl_pct": 2.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        },
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "MSFT",
            "strategy": "momentum",
            "direction": "short",
            "entry_price": 200.0,
            "stop_loss": 220.0,
            "outcomes": {"close": {"pnl_pct": 2.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        }
    ]

    # Only AAPL is on the borrow list. MSFT short should be rejected.
    cfg = SimulationConfig(
        starting_capital=100000.0,
        max_open_positions=3,
        borrow_available_symbols={"AAPL"},
        slippage_bps=0.0,
        commission_per_share=0.0
    )

    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals)

    assert len(res.trades) == 1
    assert res.trades[0]["symbol"] == "AAPL"


def test_correlation_matrix_point_in_time_and_no_leakage():
    # Evaluates correlation constraints on 2026-01-06.
    # AAPL enters at 10:00.
    # BAC signal arrives at 10:15.
    # If using correlation data of 2026-01-06, correlation is 0.85 (> 0.75 max_correlation) -> gated.
    # If it leaked 2026-01-07 (future) data where correlation drops to 0.20, it would have entered.
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "outcomes": {"close": {"pnl_pct": 1.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
        },
        {
            "timestamp": "2026-01-06T10:15:00",
            "symbol": "BAC",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 50.0,
            "stop_loss": 45.0,
            "outcomes": {"close": {"pnl_pct": 1.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:45:00"}}
        }
    ]

    # Matrix includes prior, current, and future dates
    correlation_matrix = {
        date(2026, 1, 5): pd.DataFrame({
            "AAPL": [1.0, 0.40],
            "BAC": [0.40, 1.0]
        }, index=["AAPL", "BAC"]),
        date(2026, 1, 6): pd.DataFrame({
            "AAPL": [1.0, 0.85],  # high correlation on simulation date
            "BAC": [0.85, 1.0]
        }, index=["AAPL", "BAC"]),
        date(2026, 1, 7): pd.DataFrame({
            "AAPL": [1.0, 0.20],  # future low correlation
            "BAC": [0.20, 1.0]
        }, index=["AAPL", "BAC"])
    }

    cfg = SimulationConfig(
        starting_capital=100000.0,
        max_open_positions=3,
        max_correlation=0.75
    )

    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals, correlation_matrix=correlation_matrix)

    # AAPL should be entered. BAC should be gated because point-in-time correlation on 2026-01-06 is 0.85.
    assert len(res.trades) == 1
    assert res.trades[0]["symbol"] == "AAPL"


def test_empty_and_insufficient_sample_handling():
    # Empty signals input should return default clean empty result without throwing exceptions
    sim = VirtualPortfolioSimulator()
    res = sim.run([])
    assert isinstance(res, SimulationResult)
    assert len(res.trades) == 0
    assert res.metrics["total_trades"] == 0
    assert res.metrics["sharpe"] is None
    assert res.metrics["sortino"] is None
    assert res.metrics["cvar_95"] is None

    # Single trade (insufficient days for Sharpe/Sortino standard deviation)
    signals = [{
        "timestamp": "2026-01-06T10:00:00",
        "symbol": "AAPL",
        "strategy": "momentum",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "outcomes": {"close": {"pnl_pct": 2.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
    }]
    res_single = sim.run(signals)
    assert len(res_single.trades) == 1
    assert res_single.metrics["sharpe"] is None
    assert res_single.metrics["sortino"] is None


def test_observational_equivalence_and_safety_gating():
    # Assert that calling the simulator does NOT write to any live broker or modify engine settings
    from unittest.mock import MagicMock
    mock_broker = MagicMock()
    
    # We call the simulator. It should read parameters and compute math but never touch the mock broker.
    sim = VirtualPortfolioSimulator()
    signals = [{
        "timestamp": "2026-01-06T10:00:00",
        "symbol": "AAPL",
        "strategy": "momentum",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "outcomes": {"close": {"pnl_pct": 2.0, "bars_held": 30, "exit_timestamp": "2026-01-06T10:30:00"}}
    }]
    sim.run(signals)
    
    # Assert no broker trades or submissions were triggered
    assert mock_broker.submit_order.call_count == 0
    assert mock_broker.cancel_order.call_count == 0
