"""Broker interface and shared types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.domain import (
        AccountSnapshot,
        Order,
        OrderRequest,
        Position,
    )


class BrokerTimeoutError(RuntimeError):
    """Raised when a broker HTTP call exceeds its configured wall-clock budget.

    Callers must treat this as an *ambiguous* result: the request may or may
    not have reached the broker.  They must NOT assume success or clean failure.
    """


@dataclass
class Fill:
    symbol: str
    side: str  # 'BUY' | 'SELL'
    qty: float
    price: float
    strategy: str = ""
    reason: str = ""
    order_id: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


class Broker:
    """Abstract broker interface."""

    name: str = "base"

    async def connect(self) -> None:  # pragma: no cover
        ...

    async def account(self) -> AccountSnapshot:  # pragma: no cover
        ...

    async def positions(self) -> list[Position]:  # pragma: no cover
        ...

    async def open_orders(self) -> list[Order]:  # pragma: no cover
        ...

    async def submit(self, req: OrderRequest) -> Order:  # pragma: no cover
        ...

    async def poll_fills(self) -> None:  # pragma: no cover
        ...

    def drain_fills(self) -> list[Fill]:  # pragma: no cover
        return []

    async def cancel(self, order_id: str) -> bool:  # pragma: no cover
        return False

    async def close_position(self, symbol: str, reason: str = "manual") -> Order | None:  # pragma: no cover
        return None

    async def flatten_all(self, reason: str = "flatten") -> list[Order]:  # pragma: no cover
        return []
