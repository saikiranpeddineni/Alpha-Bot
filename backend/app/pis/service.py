from __future__ import annotations

from datetime import datetime

from .models import PISDecision, PositionObservation
from .position_brain import PositionBrain


class PositionIntelligenceSystem:
    """Registry/orchestrator for per-position brains.

    The execution engine should ask this service for discretionary exit policy.
    No strategy, harvester, or challenger should independently close a live
    position once CR-290 live authority is enabled.
    """

    def __init__(self) -> None:
        self._brains: dict[str, PositionBrain] = {}

    def register(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        initial_stop: float,
        entry_time: datetime,
        strategy: str = "unknown",
    ) -> PositionBrain:
        key = symbol.upper()
        brain = PositionBrain(
            symbol=key,
            side=side,
            entry_price=entry_price,
            initial_stop=initial_stop,
            entry_time=entry_time,
            strategy=strategy,
        )
        self._brains[key] = brain
        return brain

    def get(self, symbol: str) -> PositionBrain | None:
        return self._brains.get(symbol.upper())

    def evaluate(self, symbol: str, observation: PositionObservation) -> PISDecision:
        brain = self.get(symbol)
        if brain is None:
            raise KeyError(f"PIS has no registered PositionBrain for {symbol.upper()}")
        return brain.observe(observation)

    def close(self, symbol: str) -> None:
        brain = self._brains.pop(symbol.upper(), None)
        if brain is not None:
            brain.close()

    @property
    def brains(self) -> dict[str, PositionBrain]:
        return dict(self._brains)
