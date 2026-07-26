"""Persistent replay job/session and artifact store."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import DATA_DIR


class ReplaySessionManager:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (DATA_DIR / "replay_sessions.db")
        self.artifact_root = self.db_path.parent / "replays"
        self._active_cancels: set[str] = set()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS replay_sessions (
                replay_id TEXT PRIMARY KEY, status TEXT NOT NULL, progress_pct REAL NOT NULL,
                start_date TEXT NOT NULL, end_date TEXT NOT NULL, config_json TEXT NOT NULL,
                manifest_json TEXT, summary_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.commit()

    def create_session(self, replay_id: str, start_date: str, end_date: str, config: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""INSERT OR REPLACE INTO replay_sessions
                (replay_id,status,progress_pct,start_date,end_date,config_json,manifest_json,summary_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (replay_id, "CREATED", 0.0, start_date, end_date, json.dumps(config), None, None, now, now))
            conn.commit()
        return self.get_session(replay_id)

    def update_session(self, replay_id: str, status: str | None = None, progress_pct: float | None = None,
                       manifest: dict[str, Any] | None = None, summary: dict[str, Any] | None = None) -> None:
        fields, params = ["updated_at = ?"], [datetime.now(timezone.utc).isoformat()]
        for name, value in (("status", status), ("progress_pct", progress_pct)):
            if value is not None:
                fields.append(f"{name} = ?"); params.append(value)
        if manifest is not None:
            fields.append("manifest_json = ?"); params.append(json.dumps(manifest, default=str))
        if summary is not None:
            fields.append("summary_json = ?"); params.append(json.dumps(summary, default=str))
        params.append(replay_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE replay_sessions SET {', '.join(fields)} WHERE replay_id = ?", params); conn.commit()

    def save_artifact(self, replay_id: str, name: str, payload: Any) -> Path:
        if name not in {"ledger", "timeline", "parity", "manifest"}:
            raise ValueError(f"Unsupported replay artifact {name!r}")
        directory = self.artifact_root / replay_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(path)
        return path

    def get_artifact(self, replay_id: str, name: str) -> Any | None:
        path = self.artifact_root / replay_id / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def request_cancel(self, replay_id: str) -> bool:
        self._active_cancels.add(replay_id); self.update_session(replay_id, status="CANCELLED"); return True

    def is_cancelled(self, replay_id: str) -> bool:
        return replay_id in self._active_cancels

    def get_session(self, replay_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM replay_sessions WHERE replay_id = ?", (replay_id,)).fetchone()
        if not row: return None
        return {"replay_id": row["replay_id"], "status": row["status"], "progress_pct": row["progress_pct"],
                "start_date": row["start_date"], "end_date": row["end_date"],
                "config": json.loads(row["config_json"]) if row["config_json"] else {},
                "manifest": json.loads(row["manifest_json"]) if row["manifest_json"] else None,
                "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ids = [r["replay_id"] for r in conn.execute("SELECT replay_id FROM replay_sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
        return [s for replay_id in ids if (s := self.get_session(replay_id)) is not None]
