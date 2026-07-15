"""Causal boundary for supplying bounded discovery symbols to scanner Stage 1."""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable, Iterable, Protocol

from app.core.universe_discovery import bounded_fallback


class DiscoveryResultLike(Protocol):
    shortlist: tuple[str, ...]


AcquireDiscovery = Callable[[], Awaitable[DiscoveryResultLike]]


async def resolve_stage1_symbols(
    *,
    as_of: datetime | None,
    static_symbols: Iterable[str],
    fallback_limit: int,
    discovery_supported: bool,
    acquire_discovery: AcquireDiscovery | None,
) -> tuple[str, ...]:
    """Resolve Stage-1 input while keeping current discovery out of replay.

    Any historical ``as_of=T`` path returns the static historical candidate
    surface without invoking current asset metadata or snapshot discovery.
    Live discovery is input-only and fail-closed to the same deterministic,
    bounded static fallback when unsupported, empty, malformed, or failed.
    """
    static_pool = tuple(
        sorted(
            {
                symbol.strip().upper()
                for symbol in static_symbols
                if isinstance(symbol, str) and symbol.strip()
            }
        )
    )
    if as_of is not None:
        return static_pool

    fallback = bounded_fallback(static_pool, fallback_limit)
    if not discovery_supported or acquire_discovery is None:
        return fallback

    try:
        result = await acquire_discovery()
        raw_shortlist = result.shortlist
    except Exception:  # provider transports and result shapes vary
        return fallback

    shortlist = tuple(
        sorted(
            {
                symbol.strip().upper()
                for symbol in raw_shortlist
                if isinstance(symbol, str) and symbol.strip()
            }
        )
    )
    return shortlist[:fallback_limit] if shortlist else fallback
