"""Provider-neutral helpers for bounded symbol-universe discovery."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

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
    snapshot_retries: int
    rate_limit_retries: int
    failed_batches: int
    deadline_exhausted: bool
    request_budget: int
    covered_symbols: int
    missing_symbols: int
    stale_symbols: int
    future_symbols: int
    shortlist_symbols: int
    budget_exhausted: bool


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    quotes: dict[str, Quote]
    shortlist: tuple[str, ...]
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


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        response = getattr(exc, "response", None)
        value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value is None:
        value = getattr(exc, "retry_after", None)
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


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
    seen: dict[str, bool] = {}
    normalized: list[str] = []
    for symbol in symbols:
        if (value := _normalize_symbol(symbol)) and value not in seen:
            seen[value] = True
            normalized.append(value)
    for start in range(0, len(normalized), batch_size):
        yield tuple(normalized[start : start + batch_size])


def bounded_fallback(symbols: Iterable[str], limit: int) -> tuple[str, ...]:
    """Return a deterministic bounded fallback; never expand an input universe."""
    if limit < 1:
        raise ValueError("limit must be positive")
    normalized = tuple(sorted({value for symbol in symbols if (value := _normalize_symbol(symbol))}))
    return normalized[:limit]


def coarse_shortlist(quotes: dict[str, Quote], limit: int) -> tuple[str, ...]:
    """Rank causally accepted snapshots using cheap deterministic opportunity proxies."""
    if limit < 1:
        raise ValueError("limit must be positive")
    ranked = sorted(
        quotes.items(),
        key=lambda item: (
            -(max(0.0, float(item[1].price)) * max(0.0, float(item[1].volume))),
            -abs(float(item[1].change_pct)),
            item[0],
        ),
    )
    return tuple(symbol for symbol, _quote in ranked[:limit])


class DiscoveryCoordinator:
    """Acquire broad snapshots without unbounded or non-causal provider inputs.

    Discovery only supplies a bounded symbol shortlist. Existing scanner, ML,
    risk, portfolio and execution gates remain authoritative.
    """

    def __init__(
        self,
        provider: DiscoveryProvider,
        *,
        feed: str,
        asset_cache_ttl: timedelta,
        batch_size: int,
        request_budget: int,
        max_snapshot_age: timedelta = timedelta(minutes=5),
        future_tolerance: timedelta = timedelta(0),
        shortlist_limit: int = 50,
        result_cache_ttl: timedelta = timedelta(minutes=15),
        max_retries: int = 2,
        retry_base_delay: float = 0.25,
        retry_max_delay: float = 5.0,
        deadline_seconds: float = 15.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_fn: Callable[[], float] = random.random,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if asset_cache_ttl < timedelta(0):
            raise ValueError("asset_cache_ttl must be non-negative")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if request_budget < 1:
            raise ValueError("request_budget must be positive")
        if max_snapshot_age < timedelta(0):
            raise ValueError("max_snapshot_age must be non-negative")
        if future_tolerance < timedelta(0):
            raise ValueError("future_tolerance must be non-negative")
        if result_cache_ttl < timedelta(0):
            raise ValueError("result_cache_ttl must be non-negative")
        if shortlist_limit < 1:
            raise ValueError("shortlist_limit must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_base_delay < 0 or retry_max_delay < retry_base_delay:
            raise ValueError("retry delays must be bounded and non-negative")
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        self.provider = provider
        self.feed = feed
        self.asset_cache_ttl = asset_cache_ttl
        self.batch_size = batch_size
        self.request_budget = request_budget
        self.max_snapshot_age = max_snapshot_age
        self.future_tolerance = future_tolerance
        self.shortlist_limit = shortlist_limit
        self.result_cache_ttl = result_cache_ttl
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.deadline_seconds = deadline_seconds
        self._sleeper = sleeper
        self._random = random_fn
        self._clock = clock or (lambda: datetime.now(UTC))
        self._asset_cache: tuple[str, ...] | None = None
        self._asset_cache_at: datetime | None = None
        self._cached_result: DiscoveryResult | None = None
        self._cached_result_at: datetime | None = None

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

    async def _get_quotes_with_retry(
        self,
        batch: tuple[str, ...],
        deadline: float,
        request_allowance: int,
    ) -> tuple[dict[str, Quote], int, int, bool, int]:
        retries = 0
        rate_limit_retries = 0
        attempts = 0
        loop = asyncio.get_running_loop()
        for attempt in range(self.max_retries + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {}, retries, rate_limit_retries, True, attempts
            if attempts >= request_allowance:
                return {}, retries, rate_limit_retries, False, attempts
            attempts += 1
            try:
                return (
                    await asyncio.wait_for(self.provider.get_quotes(list(batch)), timeout=remaining),
                    retries,
                    rate_limit_retries,
                    False,
                    attempts,
                )
            except TimeoutError:
                return {}, retries, rate_limit_retries, True, attempts
            except Exception as exc:  # noqa: BLE001 - provider transports vary
                status = _status_code(exc)
                retryable = status == 429 or (status is not None and 500 <= status < 600)
                if not retryable or attempt >= self.max_retries or attempts >= request_allowance:
                    return {}, retries, rate_limit_retries, False, attempts
                retries += 1
                if status == 429:
                    rate_limit_retries += 1
                retry_after = _retry_after_seconds(exc) if status == 429 else None
                exponential = min(self.retry_max_delay, self.retry_base_delay * (2**attempt))
                delay = retry_after if retry_after is not None else exponential * (0.5 + 0.5 * self._random())
                delay = min(self.retry_max_delay, max(0.0, delay))
                remaining = deadline - loop.time()
                if delay >= remaining:
                    return {}, retries, rate_limit_retries, True, attempts
                await self._sleeper(delay)
        return {}, retries, rate_limit_retries, False, attempts

    async def acquire(self) -> DiscoveryResult:
        symbols, cache_hit = await self._symbols()
        now = self._clock()
        result_cache_fresh = (
            self._cached_result_at is not None
            and now - self._cached_result_at <= self.result_cache_ttl
        )
        if cache_hit and self._cached_result is not None and result_cache_fresh:
            tel = self._cached_result.telemetry
            new_tel = DiscoveryTelemetry(
                provider=tel.provider,
                feed=tel.feed,
                asset_cache_hit=True,
                eligible_symbols=tel.eligible_symbols,
                snapshot_batches=tel.snapshot_batches,
                snapshot_requests=tel.snapshot_requests,
                snapshot_retries=tel.snapshot_retries,
                rate_limit_retries=tel.rate_limit_retries,
                failed_batches=tel.failed_batches,
                deadline_exhausted=tel.deadline_exhausted,
                request_budget=tel.request_budget,
                covered_symbols=tel.covered_symbols,
                missing_symbols=tel.missing_symbols,
                stale_symbols=tel.stale_symbols,
                future_symbols=tel.future_symbols,
                shortlist_symbols=tel.shortlist_symbols,
                budget_exhausted=tel.budget_exhausted,
            )
            return DiscoveryResult(
                quotes=self._cached_result.quotes,
                shortlist=self._cached_result.shortlist,
                telemetry=new_tel,
            )
        as_of = now
        # Shuffle symbols deterministically by day to cover the entire A-Z spectrum
        shuffled = list(symbols)
        import random
        seed_val = int(as_of.timestamp()) // 86400  # daily seed
        rng = random.Random(seed_val)
        rng.shuffle(shuffled)
        symbols = tuple(shuffled)

        quotes: dict[str, Quote] = {}
        batches = 0
        requests = 0
        retries = 0
        rate_limit_retries = 0
        failed_batches = 0
        deadline_exhausted = False
        stale = 0
        future = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.deadline_seconds
        for batch in batched_symbols(symbols, self.batch_size):
            if requests >= self.request_budget or loop.time() >= deadline:
                deadline_exhausted = loop.time() >= deadline
                break
            batches += 1
            received, batch_retries, batch_rate_limits, batch_deadline, batch_requests = await self._get_quotes_with_retry(
                batch,
                deadline,
                self.request_budget - requests,
            )
            requests += batch_requests
            retries += batch_retries
            rate_limit_retries += batch_rate_limits
            if batch_deadline:
                deadline_exhausted = True
            if not received:
                failed_batches += 1
            for symbol, quote in received.items():
                normalized = _normalize_symbol(symbol)
                if not normalized or normalized not in batch:
                    continue
                quote_ts = quote.ts
                if quote_ts.tzinfo is None:
                    quote_ts = quote_ts.replace(tzinfo=UTC)
                else:
                    quote_ts = quote_ts.astimezone(UTC)
                if quote_ts > as_of + self.future_tolerance:
                    future += 1
                    continue
                if as_of - quote_ts > self.max_snapshot_age:
                    stale += 1
                    continue
                quotes[normalized] = quote
            if deadline_exhausted:
                break

        shortlist = coarse_shortlist(quotes, self.shortlist_limit)
        covered = len(quotes)
        telemetry = DiscoveryTelemetry(
            provider=self.provider.name,
            feed=self.feed,
            asset_cache_hit=cache_hit,
            eligible_symbols=len(symbols),
            snapshot_batches=batches,
            snapshot_requests=requests,
            snapshot_retries=retries,
            rate_limit_retries=rate_limit_retries,
            failed_batches=failed_batches,
            deadline_exhausted=deadline_exhausted,
            request_budget=self.request_budget,
            covered_symbols=covered,
            missing_symbols=max(0, len(symbols) - covered - stale - future),
            stale_symbols=stale,
            future_symbols=future,
            shortlist_symbols=len(shortlist),
            budget_exhausted=requests >= self.request_budget and covered + stale + future < len(symbols),
        )
        res = DiscoveryResult(quotes=quotes, shortlist=shortlist, telemetry=telemetry)
        self._cached_result = res
        self._cached_result_at = as_of
        return res
