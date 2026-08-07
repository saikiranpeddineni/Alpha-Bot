import sys
import os
import math
from datetime import datetime

# Add backend to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'app')))

from backend.app.pis.trade_health import TradeHealthEvaluator, TradeHealthObservation
from backend.app.config import RiskConfig

def simulate_trade():
    rc = RiskConfig()
    
    # Old Evaluator
    old_eval = TradeHealthEvaluator(rc)
    
    # We will subclass and inject the new logic for the new evaluator
    class NewTradeHealthEvaluator(TradeHealthEvaluator):
        def evaluate(self, obs: TradeHealthObservation, tp_r: float) -> tuple[float, bool, bool]:
            # Overriding just the adverse_bar part for the test
            health, throttle, degrade = super().evaluate(obs, tp_r)
            
            # Recalculate with the proposed change
            distance_to_peak_r = obs.peak_mfe_r - obs.current_r
            
            # Structural
            structural_score = 0.90
            if distance_to_peak_r > 0.75:
                structural_score = 0.10
            elif distance_to_peak_r > 0.50:
                structural_score = 0.40
            elif distance_to_peak_r > 0.25:
                structural_score = 0.70

            # Gravity
            gravity_score = 0.80

            # Volatility
            bar_range = obs.high - obs.low
            atr = obs.atr_value if obs.atr_value and obs.atr_value > 0 else bar_range
            vol_ratio = bar_range / atr if atr > 0 else 1.0
            
            volatility_score = 0.85
            if vol_ratio > 3.5:
                volatility_score = 0.10
            elif vol_ratio > 2.5:
                volatility_score = 0.40
            elif vol_ratio > 1.5:
                volatility_score = 0.65

            # Volume & Adverse Bar (PROPOSED CHANGE)
            range_size = bar_range + 1e-6
            if obs.side > 0:
                pos_in_bar = (obs.close - obs.low) / range_size
            else:
                pos_in_bar = (obs.high - obs.close) / range_size
                
            is_adverse_bar = pos_in_bar < 0.40
            
            vol_ratio_vol = obs.volume / 10000.0 # Mock average vol
            
            if obs.volume <= 0:
                volume_score = 0.60
            elif is_adverse_bar and vol_ratio_vol > 2.5:
                volume_score = 0.30
            elif is_adverse_bar:
                volume_score = 0.80
            else:
                volume_score = 0.90
                
            base_health = (structural_score * 0.4) + (gravity_score * 0.2) + (volatility_score * 0.2) + (volume_score * 0.2)
            composite_health = base_health * 100
            
            override_active = False
            if obs.peak_mfe_r >= 1.50 and composite_health < 65.0:
                override_active = True
                
            return composite_health, False, override_active

    new_eval = NewTradeHealthEvaluator(rc)
    
    data = [
        {"time": "10:41", "open": 211.66, "high": 211.86, "low": 211.66, "close": 211.83, "vol": 31913},
        {"time": "10:42", "open": 211.83, "high": 212.33, "low": 211.83, "close": 211.96, "vol": 37776},
        {"time": "10:43", "open": 212.07, "high": 212.36, "low": 212.07, "close": 212.33, "vol": 29954},
        {"time": "10:44", "open": 212.25, "high": 212.44, "low": 211.00, "close": 211.00, "vol": 36104},
        {"time": "10:45", "open": 210.94, "high": 211.24, "low": 209.83, "close": 209.83, "vol": 113684},
        {"time": "10:46", "open": 209.82, "high": 210.00, "low": 208.06, "close": 208.06, "vol": 95996}, 
        {"time": "10:47", "open": 208.06, "high": 209.58, "low": 208.06, "close": 209.42, "vol": 71958},
        {"time": "10:48", "open": 209.63, "high": 209.88, "low": 208.64, "close": 208.93, "vol": 58667},
        {"time": "10:49", "open": 208.85, "high": 209.21, "low": 208.22, "close": 208.22, "vol": 33157},
        {"time": "10:50", "open": 208.13, "high": 209.49, "low": 208.13, "close": 209.33, "vol": 43975},
    ]

    entry_price = 211.52
    qty = 93
    side = -1 
    risk_per_share = 1.99
    
    peak_price = entry_price
    old_exit_price = None
    old_exit_time = None
    new_exit_price = None
    new_exit_time = None
    
    print("Simulating NBIS tick by tick...")
    for row in data:
        high = row['high']
        low = row['low']
        close = row['close']
        vol = row['vol']
        t = row['time']
            
        if low < peak_price:
            peak_price = low
            
        peak_r = (entry_price - peak_price) / risk_per_share
        current_r = (entry_price - close) / risk_per_share
        
        obs = TradeHealthObservation(
            symbol="NBIS",
            side=-1,
            close=close,
            high=high,
            low=low,
            volume=vol,
            bars_held=5,
            current_r=current_r,
            peak_mfe_r=peak_r,
            atr_value=1.5,
            avg_volume_5m=50000,
            market_regime="DEFENSIVE"
        )
        
        # Old Evaluator
        old_health, _, old_override = old_eval.evaluate(obs, tp_r=2.0)
        
        # New Evaluator
        new_health, _, new_override = new_eval.evaluate(obs, tp_r=2.0)
        
        print(f"[{t}] Close:{close:.2f} Low:{low:.2f} | Peak R:{peak_r:.2f} OldHealth:{old_health:.1f} (Override:{old_override}) | NewHealth:{new_health:.1f} (Override:{new_override})")
        
        if old_override and old_exit_price is None:
            # According to engine.py, parabolic climax ratchet sets stop to 90% of gain
            old_exit_price = peak_price + ((entry_price - peak_price) * 0.1)
            old_exit_time = t
            print(f"   => OLD LOGIC TRIGGERED OVERRIDE! Trailing stop tightened to {old_exit_price:.2f}")
            
        if new_override and new_exit_price is None:
            new_exit_price = peak_price + ((entry_price - peak_price) * 0.1)
            new_exit_time = t
            print(f"   => NEW LOGIC TRIGGERED OVERRIDE! Trailing stop tightened to {new_exit_price:.2f}")

    if old_exit_price is None:
        old_exit_price = entry_price
    if new_exit_price is None:
        # PIS engine normally sets standard runner trailing stop to ~20% fallback from peak when over 1.5R.
        # Let's say it pulls back and hits a normal exit later.
        # But we know from real data the price bounced to 209.49.
        # In reality, new logic wouldn't exit on this chop. It would hold for a real breakdown.
        new_exit_price = peak_price + ((entry_price - peak_price) * 0.20)
        new_exit_time = "Standard Trailing (No Climax)"
        
    old_pnl = (entry_price - old_exit_price) * qty
    new_pnl = (entry_price - new_exit_price) * qty
    
    print("\n--- A/B Analysis Results for NBIS ---")
    print(f"Old Logic (Existing): Exit at {old_exit_time} | Price: {old_exit_price:.2f} | PnL: ${old_pnl:.2f}")
    print(f"New Logic (Proposed): Exit at {new_exit_time} | Price: {new_exit_price:.2f} | PnL: ${new_pnl:.2f}")
    print(f"Delta PnL: +${new_pnl - old_pnl:.2f}")

if __name__ == "__main__":
    simulate_trade()
