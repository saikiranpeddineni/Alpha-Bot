"""Same-cycle entry exposure reservations.

The broker/account snapshot is authoritative at the start of a decision cycle, but
newly submitted entry orders are not guaranteed to be reflected in that snapshot
before the next candidate is evaluated. This ledger overlays active submissions
for the remainder of the cycle so risk checks cannot reuse stale capital or
exposure capacity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.domain import Order, OrderSide, OrderStatus, Position

_TERMINAL_NO_RESERVATION = {OrderStatus.CANCELED, OrderStatus.REJECTED}


# =============================================================================
# [MODIFIED 2026-10-25]: Add Buying Power Tracking to SameCycleEntryReservations
# ORIGINAL INTENT: Tracked cash only; buying_power was not tracked for same-cycle reservations.
# REASON FOR CHANGE: Enable accurate 4x DTBP margin reservations across same-cycle submissions.
# SAFE ROLLBACK: Previous class implementation preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
# @dataclass
# class SameCycleEntryReservations:
#     cash: float
#     positions: list[Position]
#     _reserved_symbols: set[str] = field(default_factory=set)
#
#     @classmethod
#     def from_snapshot(cls, cash: float, positions: list[Position]) -> SameCycleEntryReservations:
#         return cls(cash=max(0.0, cash), positions=list(positions))
#
#     def reserve(self, order: Order, reference_price: float) -> bool:
#         ...
# -----------------------------------------------------------------------------
# [NEW CODE]:
@dataclass
class SameCycleEntryReservations:
    cash: float
    positions: list[Position]
    buying_power: float = 0.0
    _reserved_symbols: set[str] = field(default_factory=set)

    @classmethod
    def from_snapshot(cls, cash: float, positions: list[Position], buying_power: float = 0.0) -> SameCycleEntryReservations:
        return cls(cash=max(0.0, cash), positions=list(positions), buying_power=max(0.0, buying_power))

    def reserve(self, order: Order, reference_price: float) -> bool:
        """Reserve an active entry submission for later candidates in this cycle."""
        if order.status in _TERMINAL_NO_RESERVATION:
            return False
        symbol = order.symbol.upper()
        if symbol in self._reserved_symbols:
            return False

        qty = abs(float(order.qty))
        if qty <= 0 or reference_price <= 0:
            return False
        signed_qty = qty if order.side == OrderSide.BUY else -qty
        self.positions.append(
            Position(
                symbol=symbol,
                qty=signed_qty,
                avg_entry_price=reference_price,
                market_price=reference_price,
                strategy=order.strategy,
            )
        )
        notional = qty * reference_price
        if order.side == OrderSide.BUY:
            self.cash = max(0.0, self.cash - notional)
        self.buying_power = max(0.0, self.buying_power - notional)
        self._reserved_symbols.add(symbol)
        return True
# =============================================================================
