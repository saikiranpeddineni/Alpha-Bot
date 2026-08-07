from dataclasses import dataclass

@dataclass
class Observation:
    close: float
    high: float
    low: float
    volume: float
    side: int

def get_health_score(obs: Observation, peak_r: float, distance_to_peak_r: float, vol_ratio: float, new_logic: bool = False) -> tuple[float, bool]:
    # 1. Structural Momentum Integrity
    structural_score = 0.90
    if distance_to_peak_r > 0.75:
        structural_score = 0.10
    elif distance_to_peak_r > 0.50:
        structural_score = 0.40
    elif distance_to_peak_r > 0.25:
        structural_score = 0.70

    # 2. Reversion Gravity (Time Decay)
    gravity_score = 0.80

    # 3. Volatility Compression / Expansion
    volatility_score = 0.85
    
    # 4. Volume Score: Price-Volume Distribution & Absorption Intelligence
    if new_logic:
        range_size = obs.high - obs.low + 1e-6
        if obs.side > 0:
            pos_in_bar = (obs.close - obs.low) / range_size
        else:
            pos_in_bar = (obs.high - obs.close) / range_size
        is_adverse_bar = pos_in_bar < 0.40
    else:
        is_adverse_bar = (obs.side > 0 and obs.close < obs.high) or (obs.side < 0 and obs.close > obs.low)
        
    if obs.volume <= 0:
        volume_score = 0.60
    elif is_adverse_bar and vol_ratio > 2.5:
        volume_score = 0.30
    elif is_adverse_bar:
        volume_score = 0.80
    else:
        volume_score = 0.90

    base_health = (structural_score * 0.4) + (gravity_score * 0.2) + (volatility_score * 0.2) + (volume_score * 0.2)
    composite_health = base_health * 100

    if new_logic:
        elasticity = max(0.5, min(1.5, volume_score / 0.80))
        giveback_pct = 0.12 * elasticity
        exit_price_modifier = giveback_pct
    else:
        exit_price_modifier = 0.10
        
    override_active = False
    if peak_r >= 1.50 and composite_health < 65.0:
        override_active = True

    return composite_health, override_active, exit_price_modifier

def simulate_trade():
    # Hardcoded NBIS minute data from 10:41 to 10:50
    data = [
        {"time": "10:41", "open": 211.66, "high": 211.86, "low": 211.66, "close": 211.83, "vol": 31913},
        {"time": "10:42", "open": 211.83, "high": 212.33, "low": 211.83, "close": 211.96, "vol": 37776},
        {"time": "10:43", "open": 212.07, "high": 212.36, "low": 212.07, "close": 212.33, "vol": 29954},
        {"time": "10:44", "open": 212.25, "high": 212.44, "low": 211.00, "close": 211.00, "vol": 36104},
        {"time": "10:45", "open": 210.94, "high": 211.24, "low": 209.83, "close": 209.83, "vol": 113684},
        {"time": "10:46", "open": 209.82, "high": 210.00, "low": 208.06, "close": 208.06, "vol": 95996}, # 208.06 low!
        {"time": "10:47", "open": 208.06, "high": 209.58, "low": 208.06, "close": 209.42, "vol": 71958},
        {"time": "10:48", "open": 209.63, "high": 209.88, "low": 208.64, "close": 208.93, "vol": 58667},
        {"time": "10:49", "open": 208.85, "high": 209.21, "low": 208.22, "close": 208.22, "vol": 33157},
        {"time": "10:50", "open": 208.13, "high": 209.49, "low": 208.13, "close": 209.33, "vol": 43975},
    ]

    entry_price = 211.52
    qty = 93
    side = -1 
    risk_per_share = 1.99 # Assuming initial risk to match ~1.74 peak R. (211.52 - 208.06) = 3.46. 3.46 / 1.99 = 1.738 R.
    
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
        distance_to_peak_r = peak_r - current_r
        
        obs = Observation(close=close, high=high, low=low, volume=vol, side=side)
        
        old_health, old_override, old_mod = get_health_score(obs, peak_r, distance_to_peak_r, vol_ratio=1.5, new_logic=False)
        new_health, new_override, new_mod = get_health_score(obs, peak_r, distance_to_peak_r, vol_ratio=1.5, new_logic=True)
        
        print(f"[{t}] Close:{close:.2f} Low:{low:.2f} | Peak R:{peak_r:.2f} Dist:{distance_to_peak_r:.2f} | OldHealth:{old_health:.1f} (Override:{old_override}) | NewHealth:{new_health:.1f} (Override:{new_override})")
        
        if old_override and old_exit_price is None:
            old_exit_price = peak_price + ((entry_price - peak_price) * old_mod) # 90% protection ratchet
            old_exit_time = t
            print(f"   => OLD LOGIC TRIGGERED OVERRIDE! Trailing stop tightened to {old_exit_price:.2f}")
            
        if new_override and new_exit_price is None:
            new_exit_price = peak_price + ((entry_price - peak_price) * new_mod)
            new_exit_time = t
            print(f"   => NEW LOGIC TRIGGERED OVERRIDE! Trailing stop tightened to {new_exit_price:.2f}")

    if old_exit_price is None:
        old_exit_price = entry_price
    if new_exit_price is None:
        # If it wasn't triggered by 10:50, let's assume it hits a trailing stop of 25% from peak 
        # (Standard runner trailing stop) at the lowest point, or continues to hold.
        # But we know NBIS hit a low of ~208.06, 25% of gain is 208.06 + (3.46 * 0.25) = 208.92
        new_exit_price = peak_price + ((entry_price - peak_price) * 0.25) 
        new_exit_time = "Later (Standard Trail)"
        
    old_pnl = (entry_price - old_exit_price) * qty
    new_pnl = (entry_price - new_exit_price) * qty
    
    print("\n--- A/B Analysis Results for NBIS ---")
    print(f"Old Logic (Existing): Exit at {old_exit_time} | Price: {old_exit_price:.2f} | PnL: ${old_pnl:.2f}")
    print(f"New Logic (Proposed): Exit at {new_exit_time} | Price: {new_exit_price:.2f} | PnL: ${new_pnl:.2f}")
    print(f"Delta PnL: +${new_pnl - old_pnl:.2f}")

if __name__ == "__main__":
    simulate_trade()
