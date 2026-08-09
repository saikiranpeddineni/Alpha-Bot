"""Broker interface, shared types, and broker factory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.domain import (
        AccountSnapshot,
        Order,
        OrderRequest,
        Position,
    )

from app.core.domain import OrderSide


class BrokerTimeoutError(RuntimeError):
    """Raised when a broker HTTP call exceeds its configured wall-clock budget.

    Callers must treat this as an *ambiguous* result: the request may or may
    not have reached the broker.  They must NOT assume success or clean failure.
    """


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: OrderSide | str  # 'BUY' | 'SELL' or OrderSide enum
    qty: float
    price: float
    commission: float = 0.0
    strategy: str = "manual"
    reason: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    signal_timestamp_utc: datetime | None = None
    decision_bar_timestamp_utc: datetime | None = None
    broker_fill_key: str | None = None
    cumulative_qty: float | None = None
    broker_mode: str | None = None
    broker_account_id: str | None = None


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

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:  # pragma: no cover
        return None

    async def poll_fills(self) -> None:  # pragma: no cover
        ...

    def drain_fills(self) -> list[Fill]:  # pragma: no cover
        return []

    async def cancel(self, order_id: str) -> bool:  # pragma: no cover
        return False

    async def cancel_all(self) -> None:  # pragma: no cover
        pass

    async def close_position(self, symbol: str, reason: str = "manual", qty: float | None = None) -> Order | None:  # pragma: no cover
        return None

    async def update_protection(self, symbol: str, stop_loss: float, side: OrderSide, qty: float) -> Order | None:  # pragma: no cover
        """Replace or create a standalone stop-loss order for an existing position."""
        return None

    async def flatten_all(self, reason: str = "flatten") -> list[Order]:  # pragma: no cover
        return []

    async def is_shortable(self, symbol: str) -> bool:  # pragma: no cover
        return True


def make_broker(broker_name: str, settings: Settings) -> Broker:
    """Factory — instantiate the correct Broker implementation by name.

    Supported names:
        ``"alpaca"``      — Live/paper Alpaca broker.
        ``"paper_sim"``   — Local in-process paper simulator (no Alpaca keys required).

    Raises
    ------
    ValueError
        When *broker_name* is not recognised.
    """
    name = broker_name.lower().strip()
    if name == "alpaca":
        # Production instances use the hardened adapter. It subclasses the
        # original AlpacaBroker so the public broker contract remains stable.
        from app.brokers.alpaca_hardened import AlpacaBroker
        return AlpacaBroker(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            paper=settings.alpaca_paper,
        )
    if name in ("paper_sim", "paper", "demo"):
        from app.brokers.paper import PaperBroker
        return PaperBroker(
            starting_cash=settings.paper_starting_cash,
            slippage_bps=settings.paper_slippage_bps,
            commission_per_share=settings.paper_commission_per_share,
        )
    raise ValueError(
        f"Unknown broker {broker_name!r}. "
        "Valid choices are: 'alpaca', 'paper_sim'."
    )
