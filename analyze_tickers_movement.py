import os
import sys
from datetime import datetime, timezone, timedelta
import pandas as pd

# Use Alpaca client from backend
sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))

from app.config import get_settings
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

def fetch_and_analyze():
    settings = get_settings()
    # Initialize Alpaca Data client
    api_key = settings.alpaca_api_key or os.getenv("ALPACA_API_KEY")
    secret_key = settings.alpaca_api_secret or os.getenv("ALPACA_API_SECRET")
    
    client = StockHistoricalDataClient(api_key, secret_key)
    
    # We want 2026-08-03 full day data (9:30 AM EDT to 4:00 PM EDT -> 13:30 to 20:00 UTC)
    start_dt = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 8, 3, 20, 30, tzinfo=timezone.utc)
    
    symbols = ["INTC", "IREN"]
    
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        start=start_dt,
        end=end_dt
    )
    
    bars = client.get_stock_bars(req)
    
    for sym in symbols:
        print(f"\n=======================================================")
        print(f"               ANALYSIS FOR {sym} (2026-08-03)")
        print(f"=======================================================")
        
        sym_bars = bars[sym]
        df = pd.DataFrame([{
            "timestamp": b.timestamp,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume
        } for b in sym_bars])
        
        # Convert timestamp to EDT (UTC-4)
        df["time_edt"] = df["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%H:%M:%S")
        
        day_open = df.iloc[0]["open"]
        day_high = df["high"].max()
        day_low = df["low"].min()
        day_close = df.iloc[-1]["close"]
        
        print(f"Day Summary:")
        print(f"  Open:  ${day_open:.2f}")
        print(f"  High:  ${day_high:.2f}")
        print(f"  Low:   ${day_low:.2f}")
        print(f"  Close: ${day_close:.2f}")
        print(f"  Day Range: {((day_high - day_low)/day_low)*100:.2f}%")
        
        print("\nKey Price Action (Around Trade Times):")
        # Print bars between 13:20 and 14:00 EDT
        sample_df = df[(df["time_edt"] >= "13:20:00") & (df["time_edt"] <= "14:15:00")]
        for _, row in sample_df.iterrows():
            print(f"  {row['time_edt']} | O: {row['open']:.2f} | H: {row['high']:.2f} | L: {row['low']:.2f} | C: {row['close']:.2f} | Vol: {row['volume']}")
            
        print("\nPrice Action After Trade (14:15 to Close):")
        later_df = df[df["time_edt"] >= "14:15:00"]
        later_high = later_df["high"].max()
        later_low = later_df["low"].min()
        later_close = later_df.iloc[-1]["close"] if not later_df.empty else day_close
        print(f"  Post-Exit High:  ${later_high:.2f}")
        print(f"  Post-Exit Low:   ${later_low:.2f}")
        print(f"  Post-Exit Close: ${later_close:.2f}")

if __name__ == "__main__":
    fetch_and_analyze()
