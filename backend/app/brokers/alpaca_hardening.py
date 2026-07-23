"""Alpaca-specific normalization helpers.

Kept separate from the broker adapter so Alpaca API semantics can be unit-tested
without constructing a TradingClient.
"""
from __future__ import annotations

from app.core.domain import OrderStatus


ALPACA_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "pending_cancel": OrderStatus.SUBMITTED,
    "pending_replace": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    # done_for_day can become executable again on the next trading day; stopped
    # is guaranteed but not yet executed; suspended is ineligible but not a
    # terminal cancellation. Keep all three tracked as live/non-terminal.
    "done_for_day": OrderStatus.SUBMITTED,
    "stopped": OrderStatus.SUBMITTED,
    "suspended": OrderStatus.SUBMITTED,
    # calculated may represent either filled or done-for-day while settlement is
    # pending. Without the preceding state, SUBMITTED is the conservative map.
    "calculated": OrderStatus.SUBMITTED,
    # The original order is terminal once Alpaca replaces it with a new order.
    "replaced": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


def normalize_order_status(raw_status: object) -> OrderStatus:
    """Map Alpaca order states without falsely declaring ambiguous states terminal."""
    value = str(raw_status).split(".")[-1].lower() if raw_status is not None else ""
    return ALPACA_STATUS_MAP.get(value, OrderStatus.SUBMITTED)


def normalize_stock_price(value: float) -> float:
    """Apply Alpaca stock sub-penny precision: 2dp >= $1, 4dp below $1."""
    price = float(value)
    return round(price, 2 if price >= 1.0 else 4)


def normalized_qty(qty: float, *, allow_fractional: bool) -> float | int:
    """Preserve fractional quantity only where the caller explicitly permits it."""
    value = float(qty)
    if value <= 0:
        raise ValueError("qty must be positive")
    if allow_fractional:
        return value
    whole = int(value)
    if whole < 1:
        raise ValueError("order requires at least one whole share")
    return whole


def asset_is_immediately_shortable(asset: object) -> bool:
    """Return True only when an Alpaca asset can be shorted without a locate.

    borrow_status is the current Alpaca field. easy_to_borrow remains a temporary
    compatibility fallback for older alpaca-py/server payloads during migration.
    Hard-to-borrow/locate-required assets deliberately fail closed because this
    bot does not implement Alpaca's locate workflow.
    """
    if not bool(getattr(asset, "shortable", False)):
        return False

    borrow_status = getattr(asset, "borrow_status", None)
    if borrow_status is not None:
        value = str(borrow_status).split(".")[-1].lower()
        return value in {"easy_to_borrow", "easy-to-borrow", "etb"}

    return bool(getattr(asset, "easy_to_borrow", False))
