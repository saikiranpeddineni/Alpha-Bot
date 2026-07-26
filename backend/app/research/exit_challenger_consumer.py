"""Exit challenger consumer fed only by canonical candidate and market-bar events."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from app.core.counterfactual import ExitCounterfactualEngine
from app.research.shadow_event import ShadowEventType, TradingObservationEvent

ExitEvidenceSink = Callable[[TradingObservationEvent, str, dict[str, Any]], None]


@dataclass
class _ExitReplayState:
    candidate: TradingObservationEvent
    rows: list[dict[str, Any]] = field(default_factory=list)
    index: list[Any] = field(default_factory=list)


class CanonicalExitChallengerConsumer:
    """Runs existing alternative exit policies after a causal 60-minute path is observed."""

    def __init__(self, sink: ExitEvidenceSink) -> None:
        self.sink = sink
        self._states: dict[str, _ExitReplayState] = {}
        self._by_symbol: dict[str, set[str]] = {}
        self.replays = 0
        self.skipped_missing_risk = 0
        self.out_of_order_bars = 0

    def consume(self, event: TradingObservationEvent) -> None:
        if event.event_type == ShadowEventType.CANDIDATE:
            self._register(event)
        elif event.event_type == ShadowEventType.MARKET_BAR:
            self._bar(event)

    def _register(self, event: TradingObservationEvent) -> None:
        if not event.candidate_id or event.price is None or event.stop_loss is None:
            self.skipped_missing_risk += 1
            return
        side = (event.side or "").lower()
        if side not in {"long", "short", "enter_long", "enter_short", "buy", "sell"}:
            return
        if event.candidate_id in self._states:
            return
        state = _ExitReplayState(candidate=event)
        state.index.append(event.ts)
        state.rows.append({"open": event.price, "high": event.price, "low": event.price,
                           "close": event.price, "atr14": event.atr})
        self._states[event.candidate_id] = state
        self._by_symbol.setdefault(event.symbol.upper(), set()).add(event.candidate_id)

    def _remove(self, state: _ExitReplayState) -> None:
        candidate_id = state.candidate.candidate_id
        if not candidate_id:
            return
        self._states.pop(candidate_id, None)
        symbol = state.candidate.symbol.upper()
        symbol_ids = self._by_symbol.get(symbol)
        if symbol_ids is not None:
            symbol_ids.discard(candidate_id)
            if not symbol_ids:
                self._by_symbol.pop(symbol, None)

    def _bar(self, bar: TradingObservationEvent) -> None:
        for candidate_id in tuple(self._by_symbol.get(bar.symbol.upper(), ())):
            state = self._states.get(candidate_id)
            if state is None:
                continue
            if bar.ts <= state.candidate.ts:
                self.out_of_order_bars += 1
                continue
            if None in (bar.open, bar.high, bar.low, bar.close):
                continue
            state.index.append(bar.ts)
            state.rows.append({"open": bar.open, "high": bar.high, "low": bar.low,
                               "close": bar.close, "atr14": bar.atr})
            elapsed = (bar.ts - state.candidate.ts).total_seconds() / 60.0
            if elapsed >= 60.0:
                self._replay(state)

    def _replay(self, state: _ExitReplayState) -> None:
        event = state.candidate
        initial_atr = event.atr
        if initial_atr is None or initial_atr <= 0:
            self.skipped_missing_risk += 1
            self._remove(state)
            return
        try:
            df = pd.DataFrame(state.rows, index=pd.DatetimeIndex(state.index))
            side = "short" if (event.side or "").lower() in {"short", "enter_short", "sell"} else "long"
            results = ExitCounterfactualEngine.replay_all_policies(
                df=df, entry_idx=0, entry_price=float(event.price), side=side,
                initial_stop=float(event.stop_loss), initial_atr=float(initial_atr),
            )
            for policy, evidence in results.items():
                self.sink(event, policy, evidence)
            self.replays += 1
        finally:
            # Research state must remain bounded even when a challenger or evidence sink fails.
            self._remove(state)

    def telemetry(self) -> dict[str, Any]:
        return {"active": len(self._states), "replays": self.replays,
                "skipped_missing_risk": self.skipped_missing_risk, "out_of_order_bars": self.out_of_order_bars}
