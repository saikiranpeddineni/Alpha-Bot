from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.scanner_discovery_input import resolve_stage1_symbols


@pytest.mark.asyncio
async def test_equivalent_live_shortlists_resolve_identically_regardless_of_provider_order():
    async def resolve(shortlist):
        return await resolve_stage1_symbols(
            as_of=None,
            static_symbols=("AAPL",),
            fallback_limit=3,
            discovery_supported=True,
            acquire_discovery=AsyncMock(return_value=SimpleNamespace(shortlist=shortlist)),
        )

    assert await resolve(("CCC", "AAA", "BBB")) == await resolve(("BBB", "CCC", "AAA")) == ("AAA", "BBB", "CCC")
