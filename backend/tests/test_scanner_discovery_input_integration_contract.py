from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.scanner_discovery_input import resolve_stage1_symbols


@pytest.mark.asyncio
async def test_discovery_cannot_expand_stage1_input_past_fallback_cap():
    acquire = AsyncMock(return_value=SimpleNamespace(shortlist=tuple(f"SYM{i:03d}" for i in range(200))))

    symbols = await resolve_stage1_symbols(
        as_of=None,
        static_symbols=("AAPL", "MSFT"),
        fallback_limit=50,
        discovery_supported=True,
        acquire_discovery=acquire,
    )

    assert len(symbols) == 50
    assert symbols == tuple(sorted(symbols))
