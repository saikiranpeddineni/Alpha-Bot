"""TradingRuntime -- Dependency injection container for TradingEngine.

Encapsulates clock, broker, data manager, and replay ledger dependencies
so TradingEngine does not rely on mutable attribute patching.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.brokers.base import Broker
    from app.core.clock import MarketClock
    from app.data.manager import DataManager


class TradingRuntime:
    """Dependency container for TradingEngine runtime services."""

    def __init__(
        self,
        clock: MarketClock | None = None,
        broker: Broker | None = None,
        data: DataManager | None = None,
        ledger: Any | None = None,
    ) -> None:
        self.clock = clock
        self.broker = broker
        self.data = data
        self.ledger = ledger
