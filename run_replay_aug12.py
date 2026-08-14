import sys
import asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

from app.replay.runtime import ReplayRuntime
from app.replay.session import ReplaySessionManager

async def run_test_replay():
    manager = ReplaySessionManager()
    replay_id = manager.create_session("2026-08-12", "2026-08-12", "test_run_aug12", config={})
    print(f"Created replay session: {replay_id}")
    
    runtime = ReplayRuntime(replay_id)
    try:
        ledger = await runtime.run_replay_async("2026-08-12", "2026-08-12")
        print(f"Replay completed! Production ticks: {ledger.production_ticks}")
    except Exception as e:
        print(f"Replay failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_test_replay())
