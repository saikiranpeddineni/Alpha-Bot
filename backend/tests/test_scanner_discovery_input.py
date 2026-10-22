from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.scanner_discovery_input import resolve_stage1_symbols

HISTORICAL_T = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
STATIC = ("ZZZ", "AAA", "BBB", "CCC")


@pytest.mark.asyncio
async def test_historical_as_of_makes_zero_current_discovery_calls():
    acquire = AsyncMock(side_effect=AssertionError("current discovery reached replay"))

    symbols = await resolve_stage1_symbols(
        as_of=HISTORICAL_T,
        static_symbols=STATIC,
        fallback_limit=2,
        discovery_supported=True,
        acquire_discovery=acquire,
    )

    assert symbols == ("AAA", "BBB", "CCC", "ZZZ")
    acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_shortlist_is_bounded_normalized_stage1_input():
    acquire = AsyncMock(return_value=SimpleNamespace(shortlist=("new2", " NEW1 ", "NEW2", "NEW3")))

    symbols = await resolve_stage1_symbols(
        as_of=None,
        static_symbols=STATIC,
        fallback_limit=2,
        discovery_supported=True,
        acquire_discovery=acquire,
    )

    assert symbols == ("NEW1", "NEW2")
    acquire.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_live_discovery_reserves_bounded_slots_for_core_symbols():
    acquire = AsyncMock(
        return_value=SimpleNamespace(shortlist=("NEW4", "NEW2", "NEW3", "NEW1"))
    )

    symbols = await resolve_stage1_symbols(
        as_of=None,
        static_symbols=("AAA",),
        core_symbols=(" core2 ", "NEW2", "CORE1"),
        fallback_limit=4,
        discovery_supported=True,
        acquire_discovery=acquire,
    )

    assert symbols == ("CORE1", "CORE2", "NEW1", "NEW2")


@pytest.mark.asyncio
async def test_historical_core_symbols_are_causal_static_input_only():
    acquire = AsyncMock(side_effect=AssertionError("current discovery reached replay"))

    symbols = await resolve_stage1_symbols(
        as_of=HISTORICAL_T,
        static_symbols=("AAA",),
        core_symbols=("USER1",),
        fallback_limit=1,
        discovery_supported=True,
        acquire_discovery=acquire,
    )

    assert symbols == ("AAA", "USER1")
    acquire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [SimpleNamespace(shortlist=()), RuntimeError("provider down")])
async def test_empty_or_failed_live_discovery_uses_same_deterministic_bounded_fallback(result):
    acquire = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)

    symbols = await resolve_stage1_symbols(
        as_of=None,
        static_symbols=STATIC,
        fallback_limit=2,
        discovery_supported=True,
        acquire_discovery=acquire,
    )

    assert symbols == ("AAA", "BBB")


@pytest.mark.asyncio
async def test_unsupported_provider_uses_fallback_without_discovery_call():
    acquire = AsyncMock(side_effect=AssertionError("unsupported provider was called"))

    symbols = await resolve_stage1_symbols(
        as_of=None,
        static_symbols=STATIC,
        fallback_limit=2,
        discovery_supported=False,
        acquire_discovery=acquire,
    )

    assert symbols == ("AAA", "BBB")
    acquire.assert_not_awaited()
