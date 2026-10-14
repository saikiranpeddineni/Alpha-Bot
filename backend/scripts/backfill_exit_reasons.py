"""Backfill script for legacy bare-exit trade records (CR-16).

Classifies historical trades in alphadesk.db whose exit_reason is 'exit' into
explicit attribution categories based on initial stops, targets, R-multiples,
and exit timestamps.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill")


def classify_exit(trade: dict) -> str:
    """Infer the explicit exit attribution for a trade with bare 'exit'."""
    side = trade.get("side", "long").lower()
    entry_px = float(trade.get("entry_price") or 0.0)
    exit_px = float(trade.get("exit_price") or 0.0)
    init_stop = float(trade.get("initial_stop") or 0.0) if trade.get("initial_stop") is not None else None
    r_mult = float(trade.get("r_multiple") or 0.0) if trade.get("r_multiple") is not None else None
    pnl = float(trade.get("pnl") or 0.0)
    exit_at_str = trade.get("exit_at", "")

    # Check EOD cutoff (after 15:50 ET)
    if exit_at_str:
        try:
            # Parse timestamp and check ET time
            dt_str = exit_at_str.replace("Z", "+00:00")
            from datetime import datetime
            dt_utc = datetime.fromisoformat(dt_str)
            dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
            if dt_et.hour == 15 and dt_et.minute >= 50:
                return "end of day"
        except Exception:
            pass

    # Stop Loss Check
    if init_stop is not None and init_stop > 0:
        if side in ("long", "buy") and exit_px <= (init_stop + 1e-4):
            return "stop_loss"
        if side in ("short", "sell") and exit_px >= (init_stop - 1e-4):
            return "stop_loss"

    # Take Profit / Target Hit Check
    if r_mult is not None and r_mult >= 1.5:
        return "take_profit"

    # PIS Exits vs Breakeven
    if r_mult is not None and r_mult <= -0.55:
        return "PIS Exit Reason: EARLY_THESIS_INVALIDATED,PROTECTION_BREACH"
    elif r_mult is not None and r_mult < 0.0:
        return "PIS Exit Reason: PROTECTION_BREACH"
    elif pnl > 0:
        return "PIS Exit Reason: SCALE_OUT_TARGET"
    else:
        return "manual close"


def backfill_db(db_path: Path | str = "data/alphadesk.db", dry_run: bool = False) -> int:
    db_file = Path(db_path)
    if not db_file.exists():
        logger.error(f"Database not found at {db_file}")
        return 0

    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()

    rows = cursor.execute("SELECT id, payload FROM trades").fetchall()
    updated_count = 0

    for trade_id, payload_str in rows:
        try:
            trade = json.loads(payload_str)
        except Exception:
            continue

        raw_reason = (trade.get("exit_reason") or "").strip()
        if not raw_reason or raw_reason == "exit":
            new_reason = classify_exit(trade)
            trade["exit_reason"] = new_reason
            new_payload = json.dumps(trade)

            if not dry_run:
                cursor.execute("UPDATE trades SET payload = ? WHERE id = ?", (new_payload, trade_id))
            updated_count += 1

    if not dry_run:
        conn.commit()
    conn.close()

    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Backfilled {updated_count} bare 'exit' trade records.")
    return updated_count


if __name__ == "__main__":
    backfill_db()
