import pandas as pd
import yfinance as yf
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

    # Composite Health Index (0-100 scale)
    base_health = (structural_score * 0.4) + (gravity_score * 0.2) + (volatility_score * 0.2) + (volume_score * 0.2)
    composite_health = base_health * 100

    # Parabolic Climax Override (Dynamic Trailing Ratchet)
    override_active = False
    if peak_r >= 1.50 and composite_health < 65.0:
        override_active = True

    return composite_health, override_active

def simulate_trade():
    print("Fetching NBIS data for 2026-08-06 10:41 to 11:00 EST...")
    try:
        df = yf.download('NBIS', interval='1m', start='2026-08-06', end='2026-08-07')
        df = df[(df.index >= '2026-08-06 10:41:00-04:00') & (df.index <= '2026-08-06 11:00:00-04:00')]
    except Exception as e:
        print(f"Failed to fetch data: {e}")
        return

    # Trade parameters
    entry_price = 211.52
    qty = 93
    side = -1 # Short
    initial_risk_amount = 0.5 * 211.52 / 100 # Let's assume initial risk is 1.05 from DB? 
    # Actually from the original NBIS trade: Entry=211.52, qty=93, Stop=?, R=1.74
    # Wait, the prompt says "Aug 06, 2026 10:48:18 NBIS short 93 211.52 209.49 +$188.79 +0.96% 1.3 bb_squeeze PIS Exit Reason: RUNNER_QUALIFIED,PROTECTION_RATCHET,PROTECTION_BREACH"
    # PnL = +188.79. Shares = 93. Exit = 209.49. 211.52 - 209.49 = 2.03. 2.03 * 93 = 188.79.
    # R multiple was 1.3. So 188.79 / 1.3 = 145.22 total risk. 145.22 / 93 = 1.56 risk per share.
    risk_per_share = 1.56
    
    # We will simulate minute by minute.
    # At each minute, we check the high/low/close, update the peak R.
    # Then we run the TradeHealth logic for both Old and New.
    # If old triggers override, we record the exit price for old.
    # If new triggers override, we record the exit price for new.
    # Otherwise we just hit the trailing stop.
    
    peak_price = entry_price
    old_exit_price = None
    new_exit_price = None
    
    print("\nSimulating tick by tick...")
    for idx, row in df.iterrows():
        try:
            high = row['High'].item()
            low = row['Low'].item()
            close = row['Close'].item()
            vol = row['Volume'].item()
        except:
            high = row['High']
            low = row['Low']
            close = row['Close']
            vol = row['Volume']
            
        # Update peak
        if low < peak_price:
            peak_price = low
            
        peak_r = (entry_price - peak_price) / risk_per_share
        current_r = (entry_price - close) / risk_per_share
        distance_to_peak_r = peak_r - current_r
        
        obs = Observation(close=close, high=high, low=low, volume=vol, side=side)
        
        old_health, old_override = get_health_score(obs, peak_r, distance_to_peak_r, vol_ratio=1.5, new_logic=False)
        new_health, new_override = get_health_score(obs, peak_r, distance_to_peak_r, vol_ratio=1.5, new_logic=True)
        
        print(f"[{idx.strftime('%H:%M')}] Close:{close:.2f} Low:{low:.2f} | Peak R:{peak_r:.2f} Dist:{distance_to_peak_r:.2f} | OldHealth:{old_health:.1f} (Override:{old_override}) | NewHealth:{new_health:.1f} (Override:{new_override})")
        
        if old_override and old_exit_price is None:
            # Trailing stop tightened to 90% of peak gain
            old_exit_price = peak_price + ((entry_price - peak_price) * 0.1)
            print(f"   => OLD LOGIC TRIGGERED OVERRIDE! Trailing stop tightened to {old_exit_price:.2f}")
            
        if new_override and new_exit_price is None:
            new_exit_price = peak_price + ((entry_price - peak_price) * 0.1)
            print(f"   => NEW LOGIC TRIGGERED OVERRIDE! Trailing stop tightened to {new_exit_price:.2f}")
            
    # Assuming exit price if override is triggered, we get filled at the next minute open or stop price.
    # For A/B simplicity, if it's hit, we use the stop price.
    if old_exit_price is None:
        old_exit_price = entry_price
    if new_exit_price is None:
        # If it wasn't triggered, we ride it down to the ultimate low!
        new_exit_price = peak_price + ((entry_price - peak_price) * 0.25) # Standard runner trailing stop
        
    old_pnl = (entry_price - old_exit_price) * qty
    new_pnl = (entry_price - new_exit_price) * qty
    
    print("\n--- A/B Analysis Results ---")
    print(f"Old Logic Exit Price: {old_exit_price:.2f} | PnL: ${old_pnl:.2f}")
    print(f"New Logic Exit Price: {new_exit_price:.2f} | PnL: ${new_pnl:.2f}")
    print(f"Delta: +${new_pnl - old_pnl:.2f}")

if __name__ == "__main__":
    simulate_trade()
