"""Replay runtime dependency container and one-way TradingEngine adapter.

This module is imported by deterministic replay only. It lets replay inject its clock,
broker, data manager and ledger without changing the production TradingEngine source.
Calls to TradingEngine with the normal production signature remain byte-for-byte on the
master implementation.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.brokers.base import Broker
    from app.core.clock import MarketClock
    from app.data.manager import DataManager


class TradingRuntime:
    """Dependency container used only by production-parity replay."""

    _adapter_installed = False

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
        self._install_engine_adapter()

    @classmethod
    def _install_engine_adapter(cls) -> None:
        """Allow the replay-only ``runtime=`` keyword without editing live engine code.

        The adapter is inert for every normal two-argument TradingEngine construction.
        It only substitutes dependencies when replay explicitly passes a TradingRuntime.
        """
        if cls._adapter_installed:
            return
        import app.core.engine as engine_module

        original_init = engine_module.TradingEngine.__init__
        if getattr(original_init, "_replay_runtime_adapter", False):
            cls._adapter_installed = True
            return

        def replay_compatible_init(engine_self, settings, db, runtime=None):
            original_init(engine_self, settings, db)
            if runtime is None:
                return
            engine_self.runtime = runtime
            if runtime.clock is not None:
                engine_self.clock = runtime.clock
            engine_self.data = runtime.data
            engine_self.broker = runtime.broker
            engine_self.runtime_ledger = runtime.ledger

        replay_compatible_init._replay_runtime_adapter = True
        engine_module.TradingEngine.__init__ = replay_compatible_init
        cls._adapter_installed = True
