import asyncio
import sqlite3
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "backend"))

from app.config import get_settings, RiskConfig
from app.data.manager import DataManager, make_provider
from app.ai.model import SignalModel
from app.ai.trainer import ContinuousTrainer

async def main():
    print("Starting ML Model Retraining for July 5th...")
    # Initialize dependencies
    conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
    c = conn.cursor()
    c.execute("SELECT DISTINCT symbol FROM trades")
    watchlist = [r[0] for r in c.fetchall()]
    
    # We also need the full universe of symbols from the cache for robust training
    try:
        cache_conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/bars_cache.db')
        cc = cache_conn.cursor()
        cc.execute("SELECT DISTINCT symbol FROM historical_bars")
        cache_symbols = [r[0] for r in cc.fetchall()]
        watchlist = list(set(watchlist + cache_symbols))
    except Exception as e:
        print(f"Could not load symbols from cache: {e}")
        
    print(f"Watchlist contains {len(watchlist)} symbols.")
    
    settings = get_settings()
    risk_config = settings.risk_config()
    
    provider = make_provider("yahoo", settings)
    data = DataManager(provider=provider, cache_path=Path('c:/Users/peddi/Desktop/Bots/Bot_QA/data/bars_cache.db'))
    model_dir = Path('c:/Users/peddi/Desktop/Bots/Bot_QA/data/models')
    
    model = SignalModel(model_dir, risk_config=risk_config)
    
    # Notice we pass `settings=settings` to satisfy Config Parity requirement
    trainer = ContinuousTrainer(data=data, model=model, model_dir=model_dir, settings=settings)
    
    # Train up to July 5th, 2026 to be compliant for the July 6th - July 17th replay!
    as_of = datetime(2026, 7, 5, tzinfo=timezone.utc)
    print(f"Training as of: {as_of}")
    await trainer._run_cycle(watchlist=watchlist, as_of=as_of)
    
    print("Training complete! Model artifacts saved to data/models.")

if __name__ == "__main__":
    asyncio.run(main())
