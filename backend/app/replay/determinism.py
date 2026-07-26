"""Replay-only deterministic identity helpers.

Nothing in this module is imported by the live engine. Replay may inject the
proxy into legacy production modules while a pass is executing so UUID-backed
transport identities are repeatable across Pass A/B without changing the
production implementation itself.
"""
from __future__ import annotations

import hashlib
import uuid as _uuid


class DeterministicUUIDProxy:
    """Module-like UUID proxy with a deterministic uuid4() sequence."""

    def __init__(self, seed: str) -> None:
        self.seed = str(seed)
        self.sequence = 0

    def uuid4(self) -> _uuid.UUID:
        self.sequence += 1
        digest = hashlib.sha256(f"{self.seed}:{self.sequence}".encode("utf-8")).digest()
        raw = bytearray(digest[:16])
        # Preserve RFC-4122 UUIDv4 version/variant bits so callers that validate
        # UUID shape see exactly the same contract as uuid.uuid4().
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return _uuid.UUID(bytes=bytes(raw))

    def __getattr__(self, name: str):
        return getattr(_uuid, name)
