from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.scanner_discovery_input import resolve_stage1_symbols


@pytest.mark.asyncio
async def test_malformed_live_symbols_are_rejected_instead_of_stringified():
    symbols = await resolve_stage1_symbols(
        as_of=None,
        static_symbols=("AAPL", None, 123, " MSFT "),
        fallback_limit=3,
        discovery_supported=True,
        acquire_discovery=AsyncMock(
            return_value=SimpleNamespace(shortlist=(" nvda ", None, 42, "", "NVDA", "TSLA"))
        ),
    )

    assert symbols == ("NVDA", "TSLA")


@pytest.mark.asyncio
async def test_malformed_discovery_result_shape_fails_closed_to_bounded_fallback():
    symbols = await resolve_stage1_symbols(
        as_of=None,
        static_symbols=("MSFT", "AAPL", "NVDA"),
        fallback_limit=2,
        discovery_supported=True,
        acquire_discovery=AsyncMock(return_value=SimpleNamespace()),
    )

    assert symbols == ("AAPL", "MSFT")
