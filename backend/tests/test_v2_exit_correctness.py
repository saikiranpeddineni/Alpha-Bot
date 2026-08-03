"""Unit tests for V2 Exit, Trailing Stop Monotonicity, and Intra-Bar Precedence Invariants."""

import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta

from app.analytics.adaptive_profit_harvester import (
    HarvestConfig,
    adaptive_stop,
    replay_adaptive_harvest,
)


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
