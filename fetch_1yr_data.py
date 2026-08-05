import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

# Add backend to path so we can import app
repo_root = Path(__file__).parent
sys.path.append(str(repo_root / "backend"))

from app.config import get_settings
from app.data.manager import DataManager
from app.data.alpaca_data import AlpacaDataProvider

async def fetch_1yr():
    settings = get_settings()
    # Ensure Alpaca keys are present
    if not settings.has_alpaca_keys:
        print("ERROR: Alpaca API keys missing in .env")
        return
        
    provider = AlpacaDataProvider(settings.alpaca_api_key, settings.alpaca_api_secret)
    cache_path = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
    manager = DataManager(provider=provider, cache_path=cache_path)
    
    # Connect to Master DB to get all traded/scanned symbols
    import sqlite3
    db_path = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
    conn = sqlite3.connect(db_path)
    
    symbols_set = set(["SPY", "QQQ", "^VIX", "AAPL", "NVDA", "TSLA", "MSFT", "AMD", "META", "AMZN", "BABA", "INTC", "SMCI", "IBIT", "PLTR", "SHOP", "T"])
    try:
        for row in conn.execute("SELECT DISTINCT symbol FROM trades"):
            if row[0]: symbols_set.add(row[0].upper())
        for row in conn.execute("SELECT DISTINCT symbol FROM orders"):
            if row[0]: symbols_set.add(row[0].upper())
        for row in conn.execute("SELECT DISTINCT symbol FROM shadow_ledger"):
            if row[0]: symbols_set.add(row[0].upper())
    except Exception as e:
        print("Could not fetch some symbols from db:", e)
    finally:
        conn.close()
        
    symbols = sorted(list(symbols_set))
    
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365)
    
    print(f"Fetching 1 year of 1-minute bars into {cache_path}...")
    print(f"Start: {start.strftime('%Y-%m-%d')}")
    print(f"End: {end.strftime('%Y-%m-%d')}")
    
    for sym in symbols:
        print(f"Fetching {sym}...", end="", flush=True)
        try:
            # history() fetches from Alpaca and automatically saves to bars_cache.db
            df = await manager.history(sym, "1Min", start, end, prefer_cache=False)
            print(f" cached {len(df)} bars.")
        except Exception as e:
            print(f" FAILED: {e}")

    await manager.close()
    print("Data ingestion complete!")

if __name__ == "__main__":
    asyncio.run(fetch_1yr())
