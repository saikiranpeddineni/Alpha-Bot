"""Unit tests for V2 Exit, Trailing Stop Monotonicity, and Intra-Bar Precedence Invariants."""

import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta

from app.analytics.adaptive_profit_harvester import (
    HarvestConfig,
    adaptive_stop,
    replay_adaptive_harvest,
)
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio


def test_v2_trailing_stop_monotonicity_long():
    """Verify long trailing stop only moves up (tightens) and never widens/moves backwards."""
    cfg = HarvestConfig(activation_mfe_r=1.5, activation_giveback_r=0.75, lock_floor_r=0.25)
    
    # 1. Entry at 100, initial stop at 95 (risk = 5)
    # Peak at 100 -> stop should remain initial_stop (95.0)
    d1 = adaptive_stop(direction="long", entry_price=100.0, initial_stop=95.0, prior_peak=100.0, atr=1.0, current_stop=95.0, config=cfg)
    assert d1.stop_price == 95.0
    
    # 2. Peak at 110 (MFE = 2.0R), close at 105 (giveback = 1.0R >= 0.75R) -> Confirmed giveback!
    # Lock floor = 0.25R -> stop = 100 + 0.25*5 = 101.25
    d2 = adaptive_stop(direction="long", entry_price=100.0, initial_stop=95.0, prior_peak=110.0, atr=1.0, prior_close=105.0, current_stop=95.0, config=cfg)
    assert d2.stop_price >= 101.25
    assert d2.state == "confirmed_giveback"
    
    # 3. Peak drops back to 104 -> stop MUST NOT move backwards below 101.25
    d3 = adaptive_stop(direction="long", entry_price=100.0, initial_stop=95.0, prior_peak=110.0, atr=1.0, prior_close=104.0, current_stop=d2.stop_price, config=cfg)
    assert d3.stop_price >= d2.stop_price, "Long stop moved backwards!"


def test_v2_trailing_stop_monotonicity_short():
    """Verify short trailing stop only moves down (tightens) and never widens/moves backwards."""
    cfg = HarvestConfig(activation_mfe_r=1.5, activation_giveback_r=0.75, lock_floor_r=0.25)
    
    # Short entry at 100, initial stop at 105 (risk = 5)
    # Peak (trough) at 90 (MFE = 2.0R), close at 95 (giveback = 1.0R >= 0.75R)
    # Lock floor = 0.25R -> stop = 100 - 0.25*5 = 98.75
    d1 = adaptive_stop(direction="short", entry_price=100.0, initial_stop=105.0, prior_peak=90.0, atr=1.0, prior_close=95.0, current_stop=105.0, config=cfg)
    assert d1.stop_price <= 98.75
    assert d1.state == "confirmed_giveback"
    
    # Price moves against us -> stop MUST NOT move up (loosen)
    d2 = adaptive_stop(direction="short", entry_price=100.0, initial_stop=105.0, prior_peak=90.0, atr=1.0, prior_close=97.0, current_stop=d1.stop_price, config=cfg)
    assert d2.stop_price <= d1.stop_price, "Short stop moved backwards!"


def test_intra_bar_ambiguity_stop_loss_precedence():
    """When a single bar touches both stop loss and take profit, stop loss MUST take precedence."""
    vp = SpawnVirtualPortfolio("test_ambiguity", 10000.0)
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    
    # Open LONG: entry=100, stop=95, target=110
    vp.open_virtual_position("TEST", "long", 10, 100.0, 95.0, 110.0, now)
    
    # Single candle with low=90 (breaches 95 stop) and high=115 (breaches 110 target)
    exit_rec = vp.update_ohlc_and_check_exits("TEST", o=100.0, h=115.0, l=90.0, c=92.0, now=now + timedelta(minutes=1))
    
    assert exit_rec is not None
    assert exit_rec["exit_reason"] == "STOP_LOSS", f"Expected STOP_LOSS priority, got {exit_rec['exit_reason']}"
    assert "TEST" not in vp.positions


def test_capital_release_ordering():
    """Verify position pop and capital release occur BEFORE new position open."""
    vp = SpawnVirtualPortfolio("test_cap_order", 10000.0, {"max_open_positions": 1})
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    
    # Slot 1 used by POS1
    vp.open_virtual_position("POS1", "long", 10, 100.0, 95.0, 110.0, now)
    assert vp.can_open_position("POS2") is False
    
    # Close POS1
    vp.close_virtual_position("POS1", 105.0, "TAKE_PROFIT", now + timedelta(minutes=5))
    
    # Capital and slot MUST be available for POS2 immediately
    assert "POS1" not in vp.positions
    assert vp.can_open_position("POS2") is True
    pos2 = vp.open_virtual_position("POS2", "long", 10, 50.0, 48.0, 55.0, now + timedelta(minutes=6))
    assert pos2 is not None


def test_double_exit_prevention():
    """Position pop prevents duplicate exit processing on the same symbol."""
    vp = SpawnVirtualPortfolio("test_double_exit", 10000.0)
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    vp.open_virtual_position("POS1", "long", 10, 100.0, 95.0, 110.0, now)
    
    # First exit succeeds
    rec1 = vp.close_virtual_position("POS1", 95.0, "STOP_LOSS", now + timedelta(minutes=1))
    assert rec1 is not None
    
    # Second exit attempt on closed position raises KeyError or returns None
    with pytest.raises(KeyError):
        vp.close_virtual_position("POS1", 95.0, "STOP_LOSS", now + timedelta(minutes=2))
