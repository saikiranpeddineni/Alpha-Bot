import pytest

from app.core.scanner_selection import select_sector_diverse_momentum


def test_unknown_dynamic_symbols_do_not_share_one_fake_sector_cap():
    candidates = [
        {"symbol": f"DYN{i}", "sector": "unknown"}
        for i in range(1, 9)
    ]

    selected = select_sector_diverse_momentum(candidates, max_symbols=8, max_per_sector=3)

    assert selected == [f"DYN{i}" for i in range(1, 9)]


def test_known_sector_still_obeys_diversity_cap_in_rank_order():
    candidates = [
        {"symbol": "TECH1", "sector": "technology"},
        {"symbol": "TECH2", "sector": "technology"},
        {"symbol": "TECH3", "sector": "technology"},
        {"symbol": "TECH4", "sector": "technology"},
        {"symbol": "BANK1", "sector": "financials"},
    ]

    selected = select_sector_diverse_momentum(candidates, max_symbols=8, max_per_sector=3)

    assert selected == ["TECH1", "TECH2", "TECH3", "BANK1"]


def test_missing_sector_metadata_is_symbol_scoped_and_normalized():
    candidates = [
        {"symbol": " aaa ", "sector": None},
        {"symbol": "bbb", "sector": ""},
        {"symbol": "ccc"},
    ]

    selected = select_sector_diverse_momentum(candidates, max_symbols=3, max_per_sector=1)

    assert selected == ["AAA", "BBB", "CCC"]


def test_selection_bounds_fail_closed_on_invalid_configuration():
    with pytest.raises(ValueError, match="max_symbols must be positive"):
        select_sector_diverse_momentum([], max_symbols=0, max_per_sector=3)
    with pytest.raises(ValueError, match="max_per_sector must be positive"):
        select_sector_diverse_momentum([], max_symbols=8, max_per_sector=0)
