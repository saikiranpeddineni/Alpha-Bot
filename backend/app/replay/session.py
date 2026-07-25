"""ReplaySession -- Persistent replay job session store & cancellation manager."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import DATA_DIR
from app.core.logging import applog


class ReplaySessionManager:
    """Manages durable replay sessions stored in SQLite."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (DATA_DIR / "replay_sessions.db")
        self._active_cancels: set[str] = set()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS replay_sessions (
                replay_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress_pct REAL NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                config_json TEXT NOT NULL,
                manifest_json TEXT,
                summary_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def create_session(self, replay_id: str, start_date: str, end_date: str, config: dict[str, Any]) -> dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        session = {
            "replay_id": replay_id,
            "status": "CREATED",
            "progress_pct": 0.0,
            "start_date": start_date,
            "end_date": end_date,
            "config": config,
            "manifest": None,
            "summary": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO replay_sessions
            (replay_id, status, progress_pct, start_date, end_date, config_json, manifest_json, summary_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            replay_id,
            "CREATED",
            0.0,
            start_date,
            end_date,
            json.dumps(config),
            None,
            None,
            now_iso,
            now_iso,
        ))
        conn.commit()
        conn.close()
        return session

    def update_session(
        self,
        replay_id: str,
        status: str | None = None,
        progress_pct: float | None = None,
        manifest: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        fields = ["updated_at = ?"]
        params = [now_iso]

        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if progress_pct is not None:
            fields.append("progress_pct = ?")
            params.append(progress_pct)
        if manifest is not None:
            fields.append("manifest_json = ?")
            params.append(json.dumps(manifest))
        if summary is not None:
            fields.append("summary_json = ?")
            params.append(json.dumps(summary))

        params.append(replay_id)
        sql = f"UPDATE replay_sessions SET {', '.join(fields)} WHERE replay_id = ?"
        cur.execute(sql, params)
        conn.commit()
        conn.close()

    def request_cancel(self, replay_id: str) -> bool:
        self._active_cancels.add(replay_id)
        self.update_session(replay_id, status="CANCELLED")
        return True

    def is_cancelled(self, replay_id: str) -> bool:
        return replay_id in self._active_cancels

    def get_session(self, replay_id: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM replay_sessions WHERE replay_id = ?", (replay_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "replay_id": row["replay_id"],
            "status": row["status"],
            "progress_pct": row["progress_pct"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "config": json.loads(row["config_json"]) if row["config_json"] else {},
            "manifest": json.loads(row["manifest_json"]) if row["manifest_json"] else None,
            "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM replay_sessions ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        return [
            {
                "replay_id": r["replay_id"],
                "status": r["status"],
                "progress_pct": r["progress_pct"],
                "start_date": r["start_date"],
                "end_date": r["end_date"],
                "config": json.loads(r["config_json"]) if r["config_json"] else {},
                "manifest": json.loads(r["manifest_json"]) if r["manifest_json"] else None,
                "summary": json.loads(r["summary_json"]) if r["summary_json"] else None,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
