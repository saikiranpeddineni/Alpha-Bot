from types import SimpleNamespace

import pytest

from app.brokers.alpaca_hardening import (
    asset_is_immediately_shortable,
    normalize_order_status,
    normalize_stock_price,
    normalized_qty,
)
from app.core.domain import OrderStatus


def test_documented_terminal_alpaca_statuses_do_not_look_submitted():
    for status in ("done_for_day", "replaced", "stopped", "suspended", "calculated"):
        assert normalize_order_status(status) == OrderStatus.CANCELED


def test_documented_transitional_statuses_remain_submitted():
    for status in ("pending_cancel", "pending_replace", "accepted_for_bidding"):
        assert normalize_order_status(status) == OrderStatus.SUBMITTED


def test_partially_filled_is_preserved():
    assert normalize_order_status("partially_filled") == OrderStatus.PARTIALLY_FILLED


def test_stock_price_precision_matches_alpaca_rules():
    assert normalize_stock_price(123.4567) == 123.46
    assert normalize_stock_price(0.123456) == 0.1235


def test_fractional_quantity_only_when_explicitly_allowed():
    assert normalized_qty(1.25, allow_fractional=True) == 1.25
    assert normalized_qty(3.9, allow_fractional=False) == 3
    with pytest.raises(ValueError):
        normalized_qty(0.5, allow_fractional=False)


def test_borrow_status_is_authoritative_when_present():
    etb = SimpleNamespace(shortable=True, borrow_status="easy_to_borrow", easy_to_borrow=False)
    htb = SimpleNamespace(shortable=True, borrow_status="hard_to_borrow", easy_to_borrow=True)
    assert asset_is_immediately_shortable(etb) is True
    assert asset_is_immediately_shortable(htb) is False


def test_easy_to_borrow_is_compatibility_fallback_only():
    old_asset = SimpleNamespace(shortable=True, easy_to_borrow=True)
    not_shortable = SimpleNamespace(shortable=False, easy_to_borrow=True)
    assert asset_is_immediately_shortable(old_asset) is True
    assert asset_is_immediately_shortable(not_shortable) is False
