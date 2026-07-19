import pytest
from datetime import datetime, date, UTC
import pandas as pd
import numpy as np

from app.portfolio.simulator import VirtualPortfolioSimulator, SimulationConfig, SimulationResult


def test_simulator_long_only_and_metrics():
    # 2 signals with same timestamp to test same-cycle competition and tie-breaking
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "trade_score": 80.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 2.0,   # exit_price = 102.0
                    "bars_held": 30,
                    "exit_reason": "tp",
                    "exit_timestamp": "2026-01-06T10:30:00"
                }
            }
        },
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "MSFT",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 200.0,
            "stop_loss": 196.0,
            "take_profit": 208.0,
            "trade_score": 80.0,   # tie score! Deterministic tie-break should process AAPL first (alphabetical symbol)
            "outcomes": {
                "close": {
                    "pnl_pct": -1.0,  # exit_price = 198.0
                    "bars_held": 45,
                    "exit_reason": "sl",
                    "exit_timestamp": "2026-01-06T10:45:00"
                }
            }
        }
    ]

    # Let's configure the simulator
    cfg = SimulationConfig(
        starting_capital=100000.0,
        risk_per_trade_pct=1.0,      # 1% risk per trade = $1000 risk dollars
        max_position_pct=20.0,       # Max $20,000 position
        max_open_positions=2,
        slippage_bps=0.0,            # Zero slippage for simple math
        commission_per_share=0.0,    # Zero commission
        fixed_commission_per_trade=0.0
    )
    
    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals)

    assert len(res.trades) == 2
    # Verify tie-breaker: AAPL and MSFT both entered.
    # AAPL risk distance = $2. Sizing: $1000 / $2 = 500 shares. Cost = $50,000. Under position cap (20% of equity = $20,000), it sizes down to $20,000 / 100 = 200 shares.
    # MSFT risk distance = $4. Sizing: $1000 / $4 = 250 shares. Cost = $50,000. Under position cap (20% of equity = $20,000), it sizes down to $20,000 / 200 = 100 shares.
    aapl_trade = next(t for t in res.trades if t["symbol"] == "AAPL")
    msft_trade = next(t for t in res.trades if t["symbol"] == "MSFT")

    assert aapl_trade["qty"] == 200.0
    assert msft_trade["qty"] == 100.0

    # AAPL Realized P&L = 200 * (102 - 100) = +$400
    # MSFT Realized P&L = 100 * (198 - 200) = -$200
    # Total PnL = +$200
    assert aapl_trade["realized_pnl"] == 400.0
    assert msft_trade["realized_pnl"] == -200.0
    
    assert res.metrics["total_pnl"] == 200.0
    assert res.metrics["win_rate_pct"] == 50.0
    assert res.metrics["profit_factor"] == 2.0  # GP = 400, GL = 200 -> PF = 2.0


def test_simulator_short_sale_margin_and_fees():
    # Test short position entry with margin limits and borrow fees
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "short",
            "entry_price": 100.0,
            "stop_loss": 102.0,      # risk distance = $2
            "take_profit": 96.0,
            "trade_score": 90.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 2.0,   # exit_price = 98.0 (Short gain)
                    "bars_held": 1440, # 1 day duration for borrow fee calculation
                    "exit_reason": "tp",
                    "exit_timestamp": "2026-01-07T10:00:00"
                }
            }
        }
    ]

    cfg = SimulationConfig(
        starting_capital=10000.0,    # small capital to trigger cash limits
        risk_per_trade_pct=2.0,      # $200 risk dollars
        max_position_pct=50.0,       # $5,000 position cap
        max_open_positions=1,
        short_margin_requirement=1.50, # 150% margin requirement
        borrow_fee_annual_pct=10.0,   # 10% annual borrow fee
        slippage_bps=0.0,
        commission_per_share=0.0
    )

    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals)

    assert len(res.trades) == 1
    trade = res.trades[0]
    
    # Risk-based shares: 200 / 2 = 100 shares. Value = $10,000. Margin needed = 1.5 * $10,000 = $15,000. Cash is only $10,000.
    # Cap-based shares: 50% of $10,000 = $5,000. Cost = 50 shares. Margin needed = 1.5 * $5,000 = $7,500. Cash is $10,000 (valid).
    # Buying power/margin limit: Cash $10,000 / (100 * 1.5) = 66.67 shares.
    # Therefore, sizing limits to 50.0 shares.
    assert trade["qty"] == 50.0

    # Short position entry value = 50 * 100 = $5000.
    # Exit price = 98.0.
    # Gross gain = 50 * (100 - 98) = +$100.
    # Borrow fee = $5000 * 0.10 * (1 / 365) = $1.37.
    # Net realized P&L = $100 - $1.37 = $98.63.
    assert abs(trade["realized_pnl"] - 98.63) < 0.05


def test_exit_before_entry_same_timestamp():
    # Test that exits are processed before entries at the same timestamp.
    # Starting with 1 active trade of size 100 shares (costs $10,000). Max positions = 1.
    # AAPL exit is at 10:30.
    # MSFT entry signal arrives at 10:30.
    # If exit is processed first, position slot is freed and MSFT can enter.
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 5.0,  # exit price = 105.0
                    "bars_held": 30,
                    "exit_timestamp": "2026-01-06T10:30:00"
                }
            }
        },
        {
            "timestamp": "2026-01-06T10:30:00",
            "symbol": "MSFT",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 200.0,
            "stop_loss": 180.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 1.0,
                    "bars_held": 30,
                    "exit_timestamp": "2026-01-06T11:00:00"
                }
            }
        }
    ]

    cfg = SimulationConfig(
        starting_capital=100000.0,
        risk_per_trade_pct=1.0,
        max_open_positions=1,  # Strict limit of 1 position at a time
        slippage_bps=0.0,
        commission_per_share=0.0
    )

    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals)

    # If exits were processed after entries, MSFT entry would have been rejected due to max_open_positions constraint.
    # Since exits are processed first, both AAPL and MSFT are successfully traded.
    assert len(res.trades) == 2
    symbols_traded = [t["symbol"] for t in res.trades]
    assert "AAPL" in symbols_traded
    assert "MSFT" in symbols_traded


def test_sector_and_correlation_gates():
    # Test sector concentration gating (limit = 1 per sector)
    # and correlation gating.
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",  # TECH
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 1.0,
                    "bars_held": 30,
                    "exit_timestamp": "2026-01-06T10:30:00"
                }
            }
        },
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "MSFT",  # TECH - Gated because max_positions_per_sector = 1
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 200.0,
            "stop_loss": 180.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 1.0,
                    "bars_held": 30,
                    "exit_timestamp": "2026-01-06T10:30:00"
                }
            }
        },
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "BAC",  # FINANCE - Not same sector, but we will test correlation gate on it
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 50.0,
            "stop_loss": 45.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 1.0,
                    "bars_held": 30,
                    "exit_timestamp": "2026-01-06T10:30:00"
                }
            }
        }
    ]

    # Pre-calculated correlation matrix
    correlation_matrix = {
        date(2026, 1, 6): pd.DataFrame({
            "AAPL": [1.0, 0.2, 0.85],
            "MSFT": [0.2, 1.0, 0.1],
            "BAC": [0.85, 0.1, 1.0]  # AAPL vs BAC correlation is 0.85 (> 0.75 max_correlation)
        }, index=["AAPL", "MSFT", "BAC"])
    }

    cfg = SimulationConfig(
        starting_capital=100000.0,
        max_open_positions=3,
        max_positions_per_sector=1,  # TECH limit
        max_correlation=0.75
    )

    sim = VirtualPortfolioSimulator(cfg)
    res = sim.run(signals, correlation_matrix=correlation_matrix)

    # AAPL enters first (highest alphabetically if score matches, or highest index).
    # MSFT is gated due to sector limit (already 1 TECH).
    # BAC is gated due to correlation limit with AAPL (0.85 > 0.75).
    # Total trades executed should be exactly 1 (AAPL).
    assert len(res.trades) == 1
    assert res.trades[0]["symbol"] == "AAPL"


def test_inferred_exit_timestamp_flag():
    signals = [
        {
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 1.0,
                    "bars_held": 35,
                    # exit_timestamp is missing -> should infer exit_timestamp = entry_timestamp + 35 minutes
                }
            }
        }
    ]

    sim = VirtualPortfolioSimulator()
    res = sim.run(signals)

    assert len(res.trades) == 1
    trade = res.trades[0]
    assert trade["exit_timestamp_inferred"] is True
    assert trade["exit_time"] == datetime(2026, 1, 6, 10, 35)
