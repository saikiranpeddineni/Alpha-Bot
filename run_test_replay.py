import sys
from pathlib import Path
sys.path.insert(0, str(Path('backend').absolute()))

import asyncio
from app.replay.runtime import ReplayRuntime
from app.config import Settings
from app.db.database import Database

async def main():
    db = Database(Path('data/alphadesk.db'))
    engine = ReplayRuntime(Settings(), db)
    # The run_replay_async returns a ReplayLedger object
    ledger = await engine.run_replay_async('2026-08-07', '2026-08-07', None)
    
    # We can inspect engine.replay_id for the replay ID since we injected it or let it generate
    print('Replay id:', engine.replay_id)
    
    # Now that it returned ReplayLedger, let's see some quick stats:
    if hasattr(ledger, "total_trades"):
        print("Total Replay Trades:", ledger.total_trades)
    else:
        print("Replay completed successfully.")
    
if __name__ == "__main__":
    asyncio.run(main())
