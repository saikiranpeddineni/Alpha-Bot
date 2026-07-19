from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.scanner_discovery_input import resolve_stage1_symbols


@pytest.mark.asyncio
async def test_duplicate_discovery_symbols_do_not_spend_multiple_stage1_slots():
    symbols = await resolve_stage1_symbols(
        as_of=None,
        static_symbols=("AAPL",),
        fallback_limit=2,
        discovery_supported=True,
        acquire_discovery=AsyncMock(return_value=SimpleNamespace(shortlist=("AAA", "AAA", "BBB", "CCC"))),
    )

    assert symbols == ("AAA", "BBB")
