import pytest

from app.core.scanner_discovery_input import resolve_stage1_symbols


@pytest.mark.asyncio
async def test_invalid_fallback_limit_fails_before_live_discovery():
    with pytest.raises(ValueError, match="limit must be positive"):
        await resolve_stage1_symbols(
            as_of=None,
            static_symbols=("AAPL",),
            fallback_limit=0,
            discovery_supported=False,
            acquire_discovery=None,
        )
