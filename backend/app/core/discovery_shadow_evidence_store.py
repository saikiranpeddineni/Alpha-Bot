"""Durable JSONL codec/store for causally recorded discovery shadow evidence.

The store is validation-only. It never calls a market-data provider and has no
scanner, risk, broker, execution, or live-decision authority.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from app.core.discovery_shadow_validation import ShadowSessionEvidence


_SCHEMA_VERSION = 1


def _symbols(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(symbol, str) or not symbol for symbol in value):
        raise ValueError(f"{field} must be a list of non-empty symbols")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} must not contain duplicates")
    return frozenset(value)


def _count(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _latency(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite non-negative number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return numeric


def encode_shadow_session(row: ShadowSessionEvidence) -> str:
    """Encode one validated evidence row deterministically."""
    payload = asdict(row)
    for field in ("fixed_candidates", "dynamic_candidates", "fixed_qualified", "dynamic_qualified"):
        payload[field] = sorted(payload[field])
    payload = {"schema_version": _SCHEMA_VERSION, **payload}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode_shadow_session(line: str) -> ShadowSessionEvidence:
    """Decode one row and fail closed on unknown or malformed evidence."""
    try:
        payload = json.loads(line)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid shadow evidence JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported shadow evidence schema")
    expected = {
        "schema_version", "session", "fixed_candidates", "dynamic_candidates",
        "fixed_qualified", "dynamic_qualified", "fixed_requests", "dynamic_requests",
        "fixed_latency_ms", "dynamic_latency_ms", "dynamic_missing", "dynamic_stale",
    }
    if set(payload) != expected:
        raise ValueError("shadow evidence fields do not match schema")
    if not isinstance(payload["session"], str):
        raise ValueError("session must be a string")
    try:
        return ShadowSessionEvidence(
            session=payload["session"],
            fixed_candidates=_symbols(payload["fixed_candidates"], "fixed_candidates"),
            dynamic_candidates=_symbols(payload["dynamic_candidates"], "dynamic_candidates"),
            fixed_qualified=_symbols(payload["fixed_qualified"], "fixed_qualified"),
            dynamic_qualified=_symbols(payload["dynamic_qualified"], "dynamic_qualified"),
            fixed_requests=_count(payload["fixed_requests"], "fixed_requests"),
            dynamic_requests=_count(payload["dynamic_requests"], "dynamic_requests"),
            fixed_latency_ms=_latency(payload["fixed_latency_ms"], "fixed_latency_ms"),
            dynamic_latency_ms=_latency(payload["dynamic_latency_ms"], "dynamic_latency_ms"),
            dynamic_missing=_count(payload["dynamic_missing"], "dynamic_missing"),
            dynamic_stale=_count(payload["dynamic_stale"], "dynamic_stale"),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid shadow evidence values") from exc


def load_shadow_sessions(path: Path) -> tuple[ShadowSessionEvidence, ...]:
    """Load a complete evidence corpus; one bad row rejects the whole corpus."""
    if not path.exists():
        return ()
    rows: list[ShadowSessionEvidence] = []
    sessions: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                row = decode_shadow_session(raw_line)
            except ValueError as exc:
                raise ValueError(f"invalid shadow evidence at line {line_number}: {exc}") from exc
            if row.session in sessions:
                raise ValueError(f"duplicate shadow session at line {line_number}")
            sessions.add(row.session)
            rows.append(row)
    return tuple(rows)


def append_shadow_session(path: Path, row: ShadowSessionEvidence) -> None:
    """Append one fsync'd row, rejecting duplicate session identity first."""
    existing = load_shadow_sessions(path)
    if any(item.session == row.session for item in existing):
        raise ValueError("shadow session already recorded")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encode_shadow_session(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def encode_shadow_corpus(rows: Iterable[ShadowSessionEvidence]) -> str:
    """Encode an in-memory corpus after enforcing unique session identity."""
    materialized = tuple(rows)
    sessions = [row.session for row in materialized]
    if len(sessions) != len(set(sessions)):
        raise ValueError("shadow sessions must be unique")
    return "".join(encode_shadow_session(row) + "\n" for row in materialized)
