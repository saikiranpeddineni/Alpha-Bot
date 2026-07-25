"""Unit tests for Spawn Virtual Portfolio accounting, determinism, and checkpointing."""
import pytest
from datetime import datetime, timezone, timedelta

from app.research.spawn.spawn_virtual_portfolio import (
    SpawnVirtualPortfolio,
    VirtualPosition,
)


def test_virtual_portfolio_initialization_and_sizing():
    vp = SpawnVirtualPortfolio(
        policy_id="spawn_g0_seed_01",
        seed_capital=10000.0,
        risk_config={
            "risk_per_trade_pct": 1.0,
            "max_position_pct": 25.0,
            "max_open_positions": 2,
            "allow_fractional": False
        }
    )
    
    assert vp.cash == 10000.0
    assert vp.can_open_position("AAPL") is True
    
    # Calculate position size for $150 stock with $1.50 risk ($148.50 stop)
    # Risk per trade = 1.0% of $10,000 = $100.
    # $100 / $1.50 risk = 66 shares. Max position = 25% of $10,000 = $2,500 / $150 = 16 shares.
    qty = vp.calculate_position_size(price=150.0, stop=148.50)
    assert qty == 16.0


def test_watermark_determinism():
    vp = SpawnVirtualPortfolio("spawn_g0_seed_01", seed_capital=10000.0)
    now = datetime.now(timezone.utc)
    
    # Open long position on AAPL
    vp.open_virtual_position("AAPL", "long", qty=10.0, price=150.0, stop=145.0, target=160.0, now=now)
    assert "AAPL" in vp.positions
    
    # Bar 1: Price goes down to $148 (no stop hit)
    res1 = vp.update_ohlc_and_check_exits("AAPL", o=150.0, h=151.0, l=148.0, c=149.0, now=now)
    assert res1 is None  # Position remains open
    
    # Duplicate Bar 1 with same timestamp (watermark determinism check)
    res_dup = vp.update_ohlc_and_check_exits("AAPL", o=150.0, h=151.0, l=140.0, c=140.0, now=now)
    assert res_dup is None  # Ignored due to watermark determinism! Position not stopped!


def test_virtual_position_lifecycle():
    vp = SpawnVirtualPortfolio("spawn_g0_seed_01", seed_capital=10000.0)
    now = datetime.now(timezone.utc)
    
    # Open position
    pos = vp.open_virtual_position("MSFT", "long", qty=20.0, price=300.0, stop=290.0, target=320.0, now=now)
    assert pos.symbol == "MSFT"
    assert pos.entry_notional == 6000.0
    
    # Mark to market at $310.0 (+ $200 unrealized)
    mtm = vp.mark_to_market({"MSFT": 310.0})
    assert mtm == 10200.0
    
    # Bar 2: Target hit at $320.0
    t2 = now + timedelta(minutes=1)
    exit_rec = vp.update_ohlc_and_check_exits("MSFT", o=310.0, h=321.0, l=310.0, c=320.0, now=t2)
    
    assert exit_rec is not None
    assert exit_rec["exit_reason"] == "TAKE_PROFIT"
    assert exit_rec["pnl"] == 400.0
    assert vp.cash == 10400.0
    assert "MSFT" not in vp.positions


def test_checkpoint_serialization_and_restore():
    vp = SpawnVirtualPortfolio("spawn_g0_seed_01", seed_capital=10000.0)
    now = datetime.now(timezone.utc)
    
    vp.open_virtual_position("NVDA", "long", qty=15.0, price=100.0, stop=95.0, target=110.0, now=now)
    
    checkpoint = vp.to_checkpoint_dict()
    assert checkpoint["policy_id"] == "spawn_g0_seed_01"
    assert "NVDA" in checkpoint["positions"]
    
    # Restore into fresh portfolio instance
    vp2 = SpawnVirtualPortfolio("spawn_g0_seed_01", seed_capital=10000.0)
    vp2.restore_from_checkpoint(checkpoint)
    
    assert "NVDA" in vp2.positions
    assert vp2.positions["NVDA"].qty == 15.0
    assert vp2.positions["NVDA"].entry_price == 100.0
