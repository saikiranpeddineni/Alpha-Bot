"""Provider-neutral helpers for bounded symbol-universe discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterable, Iterator, Protocol

from app.core.domain import Quote


@dataclass(frozen=True, slots=True)
class DiscoverableAsset:
    symbol: str
    active: bool
    tradable: bool
    asset_class: str = "us_equity"
    exchange: str = ""


@dataclass(frozen=True, slots=True)
class DiscoveryTelemetry:
    provider: str
    feed: str
    asset_cache_hit: bool
    eligible_symbols: int
    snapshot_batches: int
    snapshot_requests: int
    request_budget: int
    covered_symbols: int
    missing_symbols: int
    budget_exhausted: bool


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    quotes: dict[str, Quote]
    telemetry: DiscoveryTelemetry


class DiscoveryProvider(Protocol):
    name: str
    supports_asset_discovery: bool

    async def discover_assets(self) -> tuple[DiscoverableAsset, ...]: ...

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...


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


class DiscoveryCoordinator:
    """Acquire broad discovery snapshots without unbounded provider traffic.

    This component does not rank symbols or alter scanner authority. It only
    supplies bounded, deduplicated snapshot inputs and request telemetry.
    """

    def __init__(
        self,
        provider: DiscoveryProvider,
        *,
        feed: str,
        asset_cache_ttl: timedelta,
        batch_size: int,
        request_budget: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if asset_cache_ttl < timedelta(0):
            raise ValueError("asset_cache_ttl must be non-negative")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if request_budget < 1:
            raise ValueError("request_budget must be positive")
        self.provider = provider
        self.feed = feed
        self.asset_cache_ttl = asset_cache_ttl
        self.batch_size = batch_size
        self.request_budget = request_budget
        self._clock = clock or (lambda: datetime.now(UTC))
        self._asset_cache: tuple[str, ...] | None = None
        self._asset_cache_at: datetime | None = None

    async def _symbols(self) -> tuple[tuple[str, ...], bool]:
        now = self._clock()
        if (
            self._asset_cache is not None
            and self._asset_cache_at is not None
            and now - self._asset_cache_at <= self.asset_cache_ttl
        ):
            return self._asset_cache, True
        if not self.provider.supports_asset_discovery:
            return (), False
        symbols = eligible_symbols(await self.provider.discover_assets())
        self._asset_cache = symbols
        self._asset_cache_at = now
        return symbols, False

    async def acquire(self) -> DiscoveryResult:
        symbols, cache_hit = await self._symbols()
        quotes: dict[str, Quote] = {}
        batches = 0
        requests = 0
        for batch in batched_symbols(symbols, self.batch_size):
            if requests >= self.request_budget:
                break
            batches += 1
            requests += 1
            received = await self.provider.get_quotes(list(batch))
            for symbol, quote in received.items():
                normalized = _normalize_symbol(symbol)
                if normalized and normalized in batch:
                    quotes[normalized] = quote

        covered = len(quotes)
        telemetry = DiscoveryTelemetry(
            provider=self.provider.name,
            feed=self.feed,
            asset_cache_hit=cache_hit,
            eligible_symbols=len(symbols),
            snapshot_batches=batches,
            snapshot_requests=requests,
            request_budget=self.request_budget,
            covered_symbols=covered,
            missing_symbols=max(0, len(symbols) - covered),
            budget_exhausted=requests >= self.request_budget and covered < len(symbols),
        )
        return DiscoveryResult(quotes=quotes, telemetry=telemetry)
