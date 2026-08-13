import pytest
from datetime import datetime, timezone
from app.pis.models import PositionObservation, PISConfig
from app.pis.thesis_drift import ThesisDriftEvaluator


def _make_obs(r_multiple, peak_r, wick_ratio, bar_mae_r, history_len=3, side=1.0):
    entry = 100.0
    risk = 1.0
    # derived prices based on side
    if side == 1.0:
        cur_px = entry + r_multiple
        # to fake the wick ratio, we set close = cur_px. 
        # adverse wick for long is (close - low). So low = close - adverse_wick
        # bar_range = high - low
        bar_mae_px = entry + bar_mae_r
        low = min(bar_mae_px, cur_px)
        
        # wick_ratio = (close - low) / (high - low)
        # (cur_px - low) / (high - low) = wick_ratio
        # high - low = (cur_px - low) / wick_ratio
        
        adverse_wick = cur_px - low
        bar_range = adverse_wick / max(wick_ratio, 1e-6) if wick_ratio > 0 else 0.0
        high = max(cur_px, low + bar_range)
    else:
        # short side
        cur_px = entry - r_multiple
        bar_mae_px = entry - bar_mae_r
        high = max(bar_mae_px, cur_px)
        
        # adverse wick for short is (high - close)
        # wick_ratio = (high - close) / (high - low)
        adverse_wick = high - cur_px
        bar_range = adverse_wick / max(wick_ratio, 1e-6) if wick_ratio > 0 else 0.0
        low = min(cur_px, high - bar_range)
        
    return PositionObservation(
        timestamp=datetime.now(timezone.utc),
        symbol="TEST",
        side=side,
        qty=100.0,
        entry_price=entry,
        current_price=cur_px,
        high=high,
        low=low,
        close=cur_px,
        volume=1000.0,
        atr=1.0,
        initial_stop=entry - (side * 2.0),
        risk_per_share=risk,
        r_multiple=r_multiple,
        bar_high_r=0.0,
        bar_low_r=0.0,
        bar_mae_r=bar_mae_r,
        live_r=r_multiple,
    )


def test_peak_08_current_55_should_invalidate():
    # Case 1: peak +0.08R -> current -0.55R, bar_mae_r -0.60
    # Must invalidate because worst_r = -0.60, peak_r <= 0.10, adverse momentum is present (<= -0.50)
    config = PISConfig()
    invalidator = ThesisDriftEvaluator(config)
    obs = _make_obs(r_multiple=-0.55, peak_r=0.08, wick_ratio=0.0, bar_mae_r=-0.60)
    
    # We also need to inject the `bar_high_r` and `bar_low_r` to ensure `adverse_velocity` could trigger if needed.
    # But adverse_momentum (r_multiple <= -0.50) is True, so velocity doesn't matter.
    thesis = invalidator.evaluate(obs, history_len=3, peak_r=0.08, trough_r=-0.60, stagnation_bars=0)
    assert thesis.early_invalidation_detected is True


def test_peak_0_current_60_wick_02_velocity_should_invalidate():
    # Case 2: peak 0R -> current -0.60R, wick 0.02, strong adverse velocity
    config = PISConfig()
    invalidator = ThesisDriftEvaluator(config)
    obs = _make_obs(r_multiple=-0.60, peak_r=0.0, wick_ratio=0.02, bar_mae_r=-0.60)
    
    # Fake adverse velocity (abs(bar_high_r - bar_low_r) >= 0.30)
    object.__setattr__(obs, 'bar_high_r', 0.10)
    object.__setattr__(obs, 'bar_low_r', -0.60)
    
    thesis = invalidator.evaluate(obs, history_len=3, peak_r=0.0, trough_r=-0.60, stagnation_bars=0)
    assert thesis.early_invalidation_detected is True


def test_peak_08_current_55_normal_momentum_no_invalidate():
    # Case 3: peak +0.08R -> current -0.55R, normal momentum (worst_r = -0.55, not <= -0.60)
    config = PISConfig()
    invalidator = ThesisDriftEvaluator(config)
    obs = _make_obs(r_multiple=-0.55, peak_r=0.08, wick_ratio=0.0, bar_mae_r=-0.55)
    
    thesis = invalidator.evaluate(obs, history_len=3, peak_r=0.08, trough_r=-0.60, stagnation_bars=0)
    assert thesis.early_invalidation_detected is False
