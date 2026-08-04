import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
import pandas as pd
from dotenv import load_dotenv
load_dotenv()

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'app')))

from app.core.domain import Position, OrderSide
from app.data.alpaca_data import AlpacaDataProvider
from app.pis.engine_adapter import PISEngineAdapter
from app.indicators.ta import enrich

async def run_simulation():
    print("Fetching CRWV data for today...")
    data_client = AlpacaDataProvider(api_key=os.getenv("ALPACA_API_KEY"), api_secret=os.getenv("ALPACA_API_SECRET"))
    
    # Fetch all bars for today up to now
    df_full = await data_client.get_bars("CRWV", "1Min", limit=1000)
    if df_full.empty:
        print("No data fetched.")
        return

    entry_price = 78.55
    qty = 260
    initial_stop = 77.125
    take_profit = 80.83
    entry_time = datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)
    
    pos = Position(
        symbol="CRWV",
        side=OrderSide.BUY,
        qty=qty,
        avg_entry_price=entry_price,
        market_price=entry_price,
        unrealized_pl=0.0
    )
    
    pis_adapter = PISEngineAdapter()
    
    print(f"Starting simulation from {entry_time} to latest...")
    
    # Filter bars after entry
    df_post_entry = df_full[df_full.index >= entry_time]
    
    for current_time in df_post_entry.index:
        # Slice up to current time (inclusive)
        df_slice = df_full[df_full.index <= current_time].copy()
        
        # Exact engine behavior: enrich the slice
        df_enriched = enrich(df_slice)
        
        # Update position market price
        current_close = float(df_enriched["close"].iloc[-1])
        current_high = float(df_enriched["high"].iloc[-1])
        pos.market_price = current_close
        
        print(f"[{current_time}] Eval: close={current_close:.2f}, high={current_high:.2f}")
        
        # Fetch live news sentiment if provider supports it
        news_sentiment = 0.0
        if hasattr(data_client, "get_news_sentiment"):
            news_sentiment = await data_client.get_news_sentiment("CRWV")

        # Call PIS
        order, decision = await pis_adapter.evaluate_position_exit(
            position=pos,
            bar_df=df_enriched,
            initial_stop=initial_stop,
            take_profit=take_profit,
            current_stop=None,
            news_sentiment=news_sentiment,
        )
        
        health_obj = decision.health if decision else None
        overall = health_obj.overall if health_obj else 0.0
        momentum = health_obj.momentum_score if health_obj else 0.0
        
        if order:
            print(f"[{current_time}] EXIT TRIGGERED at {current_close:.2f} | Action: {decision.action if decision else 'N/A'}")
            print(f"   Reason: {order.reason}")
            print(f"   Health: Overall {overall:.2f}, Momentum {momentum:.2f}, Sentiment: {decision.health.news_sentiment if decision else 0.0:.2f}")
            break
            
        # Log significant events
        if current_close >= take_profit:
            print(f"[{current_time}] Target {take_profit} breached at {current_close:.2f}! PIS returned HOLD. Health: Overall {overall:.2f}, Sentiment: {decision.health.news_sentiment if decision else 0.0:.2f}")

    # Find the peak price achieved after entry
    peak_price = df_post_entry['high'].max()
    peak_time = df_post_entry['high'].idxmax()
    final_close = df_post_entry['close'].iloc[-1]
    
    print("\n--- SIMULATION RESULTS (WITH NEWS OVERRIDE) ---")
    print(f"Entry Price: {entry_price:.2f}")
    print(f"Max Peak Achieved: {peak_price:.2f} at {peak_time}")
    print(f"Final Close: {final_close:.2f}")
    
    max_profit = (peak_price - entry_price) * qty
    final_profit = (final_close - entry_price) * qty
    print(f"Max Potential Profit: +${max_profit:.2f}")
    print(f"Final Holding Profit: +${final_profit:.2f}")

if __name__ == "__main__":
    asyncio.run(run_simulation())
