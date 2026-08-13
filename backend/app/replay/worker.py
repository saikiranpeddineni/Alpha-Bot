import argparse
import asyncio
import os
import sys
import traceback
from datetime import datetime, timezone

from app.core.applog import applog
from app.replay.runtime import ReplayRuntime
from app.replay.session import ReplaySessionManager
from app.db.database import Database
from app.config import Settings


async def main():
    parser = argparse.ArgumentParser(description="AlphaBot Replay Worker")
    parser.add_argument("--replay-id", required=True, help="Replay session ID")
    args = parser.parse_args()

    replay_id = args.replay_id
    session_mgr = ReplaySessionManager()
    session = session_mgr.get_session(replay_id)

    if not session:
        applog.error("replay_worker", f"Session {replay_id} not found in database.")
        sys.exit(1)

    # Register worker PID
    session_mgr.update_session(replay_id, worker_pid=os.getpid())

    settings = Settings()
    db = Database(settings.resolved_db_path)

    runtime = ReplayRuntime(settings, db, replay_id=replay_id, session_manager=session_mgr)
    
    try:
        config = session["config"]
        # The runtime handles marking COMPLETED or FAILED appropriately via its own internal catches,
        # but we add a top-level catch just in case it crashes before even starting or unhandled errors escape.
        await runtime.run_replay(
            start_date=config.get("start_date"),
            end_date=config.get("end_date"),
            symbols=config.get("symbols"),
            fill_model=config.get("fill_model", "midpoint"),
            slippage_bps=config.get("slippage_bps", 0.0),
            volume_constrained=config.get("volume_constrained", False),
            max_volume_pct=config.get("max_volume_pct", 0.05),
            warmup_bars=config.get("warmup_bars_requested", 0),
            verify_parity_pass=config.get("verify_parity_pass", True)
        )
    except Exception as exc:
        applog.error("replay_worker", f"Worker unhandled exception for {replay_id}: {exc}")
        tb = traceback.format_exc()
        
        # Merge traceback into summary
        summary = session.get("summary") or {}
        summary.update({
            "error": "Replay worker unhandled exception",
            "failure_reason": "WORKER_CRASH",
            "traceback": tb
        })
        session_mgr.update_session(replay_id, status="FAILED", progress_pct=100.0, summary=summary)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
