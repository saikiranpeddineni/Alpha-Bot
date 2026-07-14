import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

backend_path = str(Path(__file__).parent.resolve())
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core.scanner import MarketScanner
from app.data.manager import DataManager, make_provider
from app.config import get_settings, DATA_DIR

async def main():
    settings = get_settings()
    provider_name = settings.resolve_data_provider("paper")
    
    db_path = DATA_DIR / "bars_cache.db"
    provider = make_provider(provider_name, settings)
    data = DataManager(provider, db_path)
    
    scanner = MarketScanner(data_manager=data, core_watchlist=["AMD", "ANET", "CBRS"])
    
    as_of = datetime(2026, 7, 6, 14, 0, 0, tzinfo=timezone.utc)
    
    print(f"Running standalone scanner for {as_of.isoformat()}")
    results = await scanner._stage1_prefilter_parallel(as_of)
    print(f"Candidates returned: {len(results)}")
    
if __name__ == "__main__":
    asyncio.run(main())
