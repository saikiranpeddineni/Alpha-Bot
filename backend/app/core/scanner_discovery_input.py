"""Causal boundary for supplying bounded discovery symbols to scanner Stage 1."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Protocol

from app.core.universe_discovery import bounded_fallback


class DiscoveryResultLike(Protocol):
    shortlist: tuple[str, ...]


AcquireDiscovery = Callable[[], Awaitable[DiscoveryResultLike]]


async def resolve_stage1_symbols(
    *,
    as_of: datetime | None,
    static_symbols: Iterable[str],
    core_symbols: Iterable[str] = (),
    fallback_limit: int,
    discovery_supported: bool,
    acquire_discovery: AcquireDiscovery | None,
) -> tuple[str, ...]:
    """Resolve Stage-1 input while keeping current discovery out of replay.

    Any historical ``as_of=T`` path returns the static historical candidate
    surface without invoking current asset metadata or snapshot discovery.
    Live discovery is input-only and fail-closed to the same deterministic,
    bounded static fallback when unsupported, empty, malformed, or failed.
    Core symbols are always retained; the live discovery portion uses the
    remaining bounded slots.
    """
    if fallback_limit < 1:
        raise ValueError("limit must be positive")

    static_pool = tuple(
        sorted(
            {
                symbol.strip().upper()
                for symbol in static_symbols
                if isinstance(symbol, str) and symbol.strip()
            }
        )
    )
    core_pool = tuple(
        sorted(
            {
                symbol.strip().upper()
                for symbol in core_symbols
                if isinstance(symbol, str) and symbol.strip()
            }
        )
    )
    core_set = set(core_pool)
    historical_pool = tuple(sorted(set(static_pool) | core_set))
    if as_of is not None:
        return historical_pool

    discovery_limit = max(0, fallback_limit - len(core_pool))
    static_fallback = bounded_fallback(
        (symbol for symbol in static_pool if symbol not in core_set),
        discovery_limit,
    ) if discovery_limit else ()
    fallback = tuple(sorted(core_set | set(static_fallback)))
    if not discovery_supported or acquire_discovery is None:
        return fallback

    try:
        result = await acquire_discovery()
        raw_shortlist = result.shortlist
        shortlist = tuple(
            sorted(
                {
                    symbol.strip().upper()
                    for symbol in raw_shortlist
                    if isinstance(symbol, str) and symbol.strip()
                }
            )
        )
    except Exception:  # provider transports and result shapes vary
        return fallback

    if not shortlist:
        return fallback

    discovered = tuple(symbol for symbol in shortlist if symbol not in core_set)
    return tuple(sorted(core_set | set(discovered[:discovery_limit])))
