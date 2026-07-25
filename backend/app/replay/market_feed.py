"""ReplayMarketFeed -- Historical bar stream driver for deterministic production replay.

Replaces real-time WebSocket feeds with a chronological 1-minute bar driver.
Streams raw market bars into DataManager and triggers production callbacks naturally.
Zero saved signal replay. Scanner and strategies run naturally on raw bars.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import pandas as pd

from app.config import DATA_DIR
from app.core.logging import applog


class ReplayMarketFeed:
    """Streams 1-minute historical bars chronologically for replay."""

    def __init__(self, data_path: Path | None = None) -> None:
        self.data_path = data_path or (DATA_DIR / "replay_bars.jsonl")
        self.symbols: set[str] = set()

    def load_bars(self, start_date: str, end_date: str, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """Load 1-minute OHLCV bars sorted in strict chronological order."""
        rows = []
        if not self.data_path.exists():
            # Fallback to scratch replay stream if data_path does not exist
            alt_path = Path(__file__).resolve().parents[3] / "scratch" / "july24_replay_stream.jsonl"
            if alt_path.exists():
                self.data_path = alt_path

        if not self.data_path.exists():
            applog.warning("replay_feed", f"Market feed data path {self.data_path} not found.")
            return []

        filter_symbols = set(s.upper() for s in symbols) if symbols else None

        with self.data_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue

                if row.get("kind") != "bar" and "open" not in row:
                    continue

                sym = str(row.get("symbol", "")).upper()
                if filter_symbols and sym not in filter_symbols:
                    continue

                ts_raw = row.get("ts") or row.get("t")
                if not ts_raw:
                    continue

                if isinstance(ts_raw, str):
                    dt = datetime.fromisoformat(ts_raw)
                elif isinstance(ts_raw, datetime):
                    dt = ts_raw
                else:
                    continue

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                dt_str = dt.strftime("%Y-%m-%d")
                if start_date and dt_str < start_date:
                    continue
                if end_date and dt_str > end_date:
                    continue

                row["ts_dt"] = dt
                row["symbol"] = sym
                rows.append(row)
                self.symbols.add(sym)

        # Sort strictly by (timestamp ASC, symbol ASC)
        rows.sort(key=lambda r: (r["ts_dt"], r["symbol"]))
        return rows
