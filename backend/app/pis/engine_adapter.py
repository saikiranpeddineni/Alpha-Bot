from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from .evidence import observation_from_market_data
from .execution import ExecutionIntent, PISExecutionAuthority
from .models import PISDecision
from .service import PositionIntelligenceSystem


@dataclass(frozen=True)
class PISEvaluation:
    decision: PISDecision
    intent: ExecutionIntent


class PISEngineAdapter:
    """Narrow integration boundary between TradingEngine and CR-290 PIS.

    TradingEngine owns orchestration and safety. PIS owns discretionary exit
    policy. This adapter deliberately exposes no entry/sizing method so PIS
    cannot drift into entry authority.
    """

    def __init__(self) -> None:
        self.pis = PositionIntelligenceSystem()
        self.execution = PISExecutionAuthority()

    @staticmethod
    def _initial_stop(position: Any, fallback: float | None = None) -> float:
        stop = getattr(position, "stop_loss", None)
        if stop is None or float(stop) <= 0:
            stop = fallback
        if stop is None or float(stop) <= 0:
            raise ValueError("PIS requires original protective stop to reconstruct 1R")
        return float(stop)

    def ensure_registered(
        self,
        *,
        symbol: str,
        position: Any,
        now: datetime,
        initial_stop: float | None = None,
    ) -> None:
        if self.pis.get(symbol) is not None:
            return
        qty = float(position.qty)
        self.pis.register(
            symbol=symbol,
            side="long" if qty > 0 else "short",
            entry_price=float(position.avg_entry_price),
            initial_stop=self._initial_stop(position, initial_stop),
            entry_time=getattr(position, "entry_time", None) or now,
            strategy=getattr(position, "strategy", "unknown") or "unknown",
        )

    def evaluate(
        self,
        *,
        symbol: str,
        position: Any,
        df: pd.DataFrame,
        now: datetime,
        initial_stop: float | None = None,
        relative_strength: float | None = None,
        market_context: float | None = None,
    ) -> PISEvaluation:
        self.ensure_registered(
            symbol=symbol,
            position=position,
            now=now,
            initial_stop=initial_stop,
        )
        observation = observation_from_market_data(
            now=now,
            position=position,
            df=df,
            relative_strength=relative_strength,
            market_context=market_context,
        )
        decision = self.pis.evaluate(symbol, observation)
        return PISEvaluation(decision=decision, intent=self.execution.translate(decision))

    def position_closed(self, symbol: str) -> None:
        self.pis.close(symbol)
