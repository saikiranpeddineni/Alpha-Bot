import sys
from pathlib import Path
sys.path.append(str(Path("backend").resolve()))

import asyncio
from app.config import Settings
from app.db.database import Database
from app.replay.runtime import ReplayRuntime

async def run_deterministic_replay():
    settings = Settings()
    # Use real DB, replay mode handles separation
    db = Database(Path("data/alphadesk_test.db"))
    runtime = ReplayRuntime(settings, db, replay_id="test_aug13_replay", data_path=Path("data/replay_bars_aug13.jsonl"))
    
    symbols = None
    print("Starting replay...")
    try:
        print("Awaiting run_replay_async...")
        res = await runtime.run_replay_async(
            start_date="2026-08-13",
            end_date="2026-08-13",
            symbols=symbols,
            fill_model="ohlc_touch",
            slippage_bps=0.0,
            verify_parity_pass=False,
            warmup_bars=0
        )
        print("Replay completed successfully.")
        
        ledger = res.get("ledger", {})
        print(f"Final Ledger Ticks: {ledger.get('production_ticks')}")
        timeline = ledger.get('timeline', [])
        print(f"Timeline entries: {len(timeline)}")
        
        import json
        for entry in timeline:
            print(json.dumps(entry, indent=2))
        
    except Exception as e:
        print(f"Replay failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_deterministic_replay())
