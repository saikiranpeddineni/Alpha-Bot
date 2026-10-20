"""Deterministic scanner selection helpers for static and discovered symbols."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def select_sector_diverse_momentum(
    candidates: Iterable[Mapping[str, object]],
    *,
    max_symbols: int,
    max_per_sector: int,
) -> list[str]:
    """Select ranked momentum symbols without inventing one shared unknown sector.

    Known sectors share their configured cap. Symbols without trustworthy sector
    metadata receive a symbol-scoped bucket, so unrelated dynamically discovered
    equities cannot suppress one another merely because metadata is absent.
    """
    if max_symbols < 1:
        raise ValueError("max_symbols must be positive")
    if max_per_sector < 1:
        raise ValueError("max_per_sector must be positive")

    selected: list[str] = []
    bucket_counts: dict[str, int] = {}
    seen_symbols: set[str] = set()
    for candidate in candidates:
        if len(selected) >= max_symbols:
            break
        symbol = str(candidate.get("symbol", "")).strip().upper()
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        sector_value = candidate.get("sector")
        sector = str(sector_value).strip().lower() if sector_value is not None else ""
        bucket = sector if sector and sector != "unknown" else f"unknown:{symbol}"
        if bucket_counts.get(bucket, 0) >= max_per_sector:
            continue
        selected.append(symbol)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    return selected
