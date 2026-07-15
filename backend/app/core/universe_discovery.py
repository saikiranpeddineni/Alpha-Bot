"""Provider-neutral helpers for bounded symbol-universe discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator


@dataclass(frozen=True, slots=True)
class DiscoverableAsset:
    symbol: str
    active: bool
    tradable: bool
    asset_class: str = "us_equity"
    exchange: str = ""


def _normalize_symbol(symbol: object) -> str | None:
    if not isinstance(symbol, str):
        return None
    normalized = symbol.strip().upper()
    return normalized or None


def eligible_symbols(assets: Iterable[DiscoverableAsset]) -> tuple[str, ...]:
    """Return a deterministic, normalized set of eligible US-equity symbols."""
    symbols: set[str] = set()
    for asset in assets:
        asset_class = asset.asset_class if isinstance(asset.asset_class, str) else ""
        symbol = _normalize_symbol(asset.symbol)
        if asset.active and asset.tradable and asset_class.lower() == "us_equity" and symbol:
            symbols.add(symbol)
    return tuple(sorted(symbols))


def batched_symbols(symbols: Iterable[str], batch_size: int) -> Iterator[tuple[str, ...]]:
    """Normalize/deduplicate symbols and yield deterministic bounded batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    normalized = tuple(sorted({value for symbol in symbols if (value := _normalize_symbol(symbol))}))
    for start in range(0, len(normalized), batch_size):
        yield normalized[start : start + batch_size]


def bounded_fallback(symbols: Iterable[str], limit: int) -> tuple[str, ...]:
    """Return a deterministic bounded fallback; never expand an input universe."""
    if limit < 1:
        raise ValueError("limit must be positive")
    normalized = tuple(sorted({value for symbol in symbols if (value := _normalize_symbol(symbol))}))
    return normalized[:limit]
