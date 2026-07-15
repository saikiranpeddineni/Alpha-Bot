"""Broker abstraction.

Add a broker by subclassing Broker and registering it in `make_broker`.
Fills flow to the engine through `drain_fills()` so position accounting and
trade records work identically for every implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.domain import (
    AccountSnapshot,
    Order,
    OrderRequest,
    OrderSide,
    Position,
    utcnow,
)


class Fill(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    qty: float
    price: float
    commission: float = 0.0
    strategy: str = "manual"
    reason: str = ""
    ts: datetime = Field(default_factory=utcnow)
    signal_timestamp_utc: datetime | None = None
    decision_bar_timestamp_utc: datetime | None = None
    # Broker-backed cumulative execution watermark.  When present, portfolio
    # accounting persists this watermark atomically with its durable checkpoint
    # and can replay only the unprocessed delta after a process restart.
    broker_fill_key: str | None = None
    cumulative_qty: float | None = None
    broker_mode: str | None = None


class Broker(ABC):
    name: str = "abstract"
    is_paper: bool = True

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    async def open_orders(self) -> list[Order]: ...

    @abstractmethod
    async def submit(self, req: OrderRequest) -> Order: ...

    @abstractmethod
    async def cancel(self, order_id: str) -> bool: ...

    @abstractmethod
    async def close_position(self, symbol: str, reason: str = "manual") -> Order | None: ...

    @abstractmethod
    async def flatten_all(self, reason: str = "flatten") -> list[Order]: ...

    @abstractmethod
    def drain_fills(self) -> list[Fill]:
        """Return fills that occurred since the last drain (and clear them)."""

    async def cancel_all(self) -> int:
        n = 0
        for order in await self.open_orders():
            if await self.cancel(order.id):
                n += 1
        return n

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


def make_broker(name: str, settings) -> Broker:  # noqa: ANN001
    """Broker factory. Register new brokers here."""
    if name == "paper_sim":
        from app.brokers.paper import PaperBroker

        return PaperBroker(
            starting_cash=settings.paper_starting_cash,
            slippage_bps=settings.paper_slippage_bps,
            commission_per_share=settings.paper_commission_per_share,
        )
    if name == "alpaca":
        from app.brokers.alpaca import AlpacaBroker

        return AlpacaBroker(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            paper=settings.alpaca_paper,
        )
    raise ValueError(f"Unknown broker {name!r}")
