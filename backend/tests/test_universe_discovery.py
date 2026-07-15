import pytest

from app.core.universe_discovery import (
    DiscoverableAsset,
    batched_symbols,
    bounded_fallback,
    eligible_symbols,
)


def test_eligible_symbols_normalizes_deduplicates_and_filters_assets():
    assets = [
        DiscoverableAsset(" aapl ", True, True),
        DiscoverableAsset("AAPL", True, True),
        DiscoverableAsset("msft", True, True),
        DiscoverableAsset("OLD", False, True),
        DiscoverableAsset("LOCKED", True, False),
        DiscoverableAsset("BTCUSD", True, True, asset_class="crypto"),
        DiscoverableAsset("", True, True),
    ]

    assert eligible_symbols(assets) == ("AAPL", "MSFT")


def test_batched_symbols_is_deterministic_deduplicated_and_capped():
    batches = list(batched_symbols(["tsla", "AAPL", " msft ", "TSLA", ""], 2))

    assert batches == [("AAPL", "MSFT"), ("TSLA",)]
    assert all(len(batch) <= 2 for batch in batches)


def test_batched_symbols_rejects_non_positive_batch_size():
    with pytest.raises(ValueError, match="batch_size must be positive"):
        list(batched_symbols(["AAPL"], 0))


def test_bounded_fallback_is_deterministic_unique_and_never_exceeds_limit():
    assert bounded_fallback(["TSLA", "aapl", "AAPL", "MSFT"], 2) == ("AAPL", "MSFT")


def test_bounded_fallback_rejects_non_positive_limit():
    with pytest.raises(ValueError, match="limit must be positive"):
        bounded_fallback(["AAPL"], 0)
