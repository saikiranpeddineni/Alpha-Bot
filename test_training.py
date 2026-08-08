import sys
from pathlib import Path
sys.path.insert(0, str(Path('backend').absolute()))

import asyncio
import logging
from datetime import datetime, UTC

# Configure stdlib logging so we see applog output
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")

from app.config import get_settings
from app.data.manager import DataManager, make_provider
from app.ai.model import SignalModel
from app.ai.trainer import ContinuousTrainer

async def main():
    settings = get_settings()
    # The user used alpaca which hits bars_cache.db
    provider = make_provider("alpaca", settings)
    data = DataManager(provider, cache_path=Path("data/bars_cache.db"))
    
    symbols = [
        "AAOI", "AMD", "AMZN", "ASTS", "AVGO", "BABA", "BE", "COHR", "CRWV", 
        "FRSH", "GOOGL", "INTC", "META", "MSFT", "MU", "NBIS", "NOK", "NOW", 
        "NVDA", "ONDS", "ORCL", "PLTR", "SKHY", "SNDK", "SPY", "TLT", "TQQQ"
    ]
    model_dir = Path("data/models")
    model = SignalModel(model_dir)
    
    trainer = ContinuousTrainer(data=data, model=model, model_dir=model_dir)
    
    print(f"Triggering training cycle for {len(symbols)} symbols...")
    await trainer._run_cycle(watchlist=symbols, as_of=datetime.now(UTC))
    
    print("Training result:", trainer.last_result)

if __name__ == "__main__":
    asyncio.run(main())
