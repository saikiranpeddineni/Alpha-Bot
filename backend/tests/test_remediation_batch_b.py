import pytest
from datetime import datetime, time
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

from app.core.calendar_helper import get_session_close
from app.core.canonical_evidence import CanonicalOutcomeResolver
from app.core.shadow_ledger import ShadowLedger
from app.db.database import Database

ET = ZoneInfo("America/New_York")


def test_market_session_calendar_helper():
    """Test #231: Session close resolver returns 16:00 ET for regular days and 13:00 ET for early close dates."""
    # Standard day: July 15, 2026 at 10:00 AM ET
    dt_standard = datetime(2026, 7, 15, 10, 0, tzinfo=ET)
    close_std = get_session_close(dt_standard)
    assert close_std.hour == 16
    assert close_std.minute == 0

    # Early close day: November 27, 2026 (Black Friday) at 10:00 AM ET
    dt_early = datetime(2026, 11, 27, 10, 0, tzinfo=ET)
    close_early = get_session_close(dt_early)
    assert close_early.hour == 13
    assert close_early.minute == 0


def test_db_driven_replay_queue_oldest_first(tmp_path):
    """Test #233: DB-driven replay queue fetches pending rejected signals ordered by ts ASC (oldest first)."""
    db_file = tmp_path / "test_batch_b.db"
    db = Database(db_file)

    # Save 3 rejected signals with different timestamps
    ts_old = datetime(2026, 7, 20, 10, 0, tzinfo=ET)
    ts_mid = datetime(2026, 7, 20, 11, 0, tzinfo=ET)
    ts_new = datetime(2026, 7, 20, 12, 0, tzinfo=ET)

    db.save_shadow_signal("cand_new", ts_new, "AAPL", "orb", "long", "demo", {"final_decision": "rejected", "replayed": False, "candidate_id": "cand_new"})
    db.save_shadow_signal("cand_old", ts_old, "AAPL", "orb", "long", "demo", {"final_decision": "rejected", "replayed": False, "candidate_id": "cand_old"})
    db.save_shadow_signal("cand_mid", ts_mid, "AAPL", "orb", "long", "demo", {"final_decision": "rejected", "replayed": False, "candidate_id": "cand_mid"})

    pending = db.get_pending_shadow_replays(mode="demo", limit=10)
    assert len(pending) == 3
    # Verify strict ASC order (cand_old -> cand_mid -> cand_new)
    assert pending[0]["candidate_id"] == "cand_old"
    assert pending[1]["candidate_id"] == "cand_mid"
    assert pending[2]["candidate_id"] == "cand_new"


def test_db_derived_replay_queue_telemetry(tmp_path):
    """Test #244: Replay queue telemetry derived directly from DB state."""
    db_file = tmp_path / "test_batch_b_telem.db"
    db = Database(db_file)

    ts1 = datetime(2026, 7, 20, 10, 0, tzinfo=ET)
    db.save_shadow_signal("cand_1", ts1, "AAPL", "orb", "long", "demo", {"final_decision": "rejected", "replayed": False})

    telemetry = db.get_replay_queue_telemetry(mode="demo", now=datetime(2026, 7, 20, 10, 30, tzinfo=ET))
    assert telemetry["replay_pending_total"] == 1
    assert telemetry["replay_oldest_pending_age_seconds"] == 1800.0


def test_failed_strategy_replay_isolation():
    """Test #230: Failed strategy replay sets replay_failed=True and invalidates evidence."""
    rec_failed = {
        "replayed": True,
        "replay_failed": True,
        "replay_failure_reason": "strategy_construction_failed",
        "simulated_pnl": 100.0,
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "regime": "RANGE",
        "regime_source": "VIX"
    }

    ev = CanonicalOutcomeResolver.resolve(rec_failed)
    assert ev.valid is False
    assert ev.invalid_reason == "REPLAY_NOT_VALID"
