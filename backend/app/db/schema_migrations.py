"""Small additive SQLite compatibility migrations run before app startup.

These migrations preserve existing local data while bringing tables created by
older AlphaDesk versions up to the schema expected by current ORM models.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


_SPAWNED_STRATEGY_COLUMNS: dict[str, str] = {
    "parent_policy_id": "VARCHAR(64)",
    "policy_version": "VARCHAR(20)",
    "config_snapshot_json": "TEXT",
    "config_fingerprint": "VARCHAR(64)",
    "created_from_candidate": "VARCHAR(64)",
    "spawn_reason": "TEXT",
    "execution_mode": "VARCHAR(20)",
    "code_commit_sha": "VARCHAR(40)",
    "model_manifest_sha": "VARCHAR(64)",
}


def migrate_spawned_strategies(path: Path) -> list[str]:
    """Add missing lineage/research columns to an existing spawn table.

    New databases are created by SQLAlchemy with the current schema, so this is
    intentionally a no-op when the database or table does not yet exist.
    ALTER TABLE ADD COLUMN is additive and does not rewrite existing rows.
    Returns the names of columns added, which makes the migration easy to test.
    """
    path = Path(path)
    if not path.exists():
        return []

    added: list[str] = []
    with sqlite3.connect(path) as conn:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spawned_strategies'"
        ).fetchone()
        if table_exists is None:
            return []

        existing = {row[1] for row in conn.execute("PRAGMA table_info(spawned_strategies)")}
        for column, sql_type in _SPAWNED_STRATEGY_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE spawned_strategies ADD COLUMN {column} {sql_type}")
                added.append(column)
        conn.commit()

    return added
