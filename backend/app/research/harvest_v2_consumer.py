"""Canonical causal Harvest-V2 consumer for ShadowAlphaBotRuntime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from app.analytics.adaptive_profit_harvester import replay_adaptive_harvest
from app.research.shadow_event import ShadowEventType, TradingObservationEvent

HarvestSink = Callable[[TradingObservationEvent, dict[str, Any]], None]


@dataclass
class _HarvestState:
    candidate: TradingObservationEvent
    rows: list[dict[str, Any]] = field(default_factory=list)
    index: list[Any] = field(default_factory=list)


class CanonicalHarvestV2Consumer:
    """Replays the existing frozen V2 policy after 60 causal post-candidate minutes.

    This replaces reliance on engine-local in-memory state for research evidence. It
    does not alter, cancel, submit, size, or otherwise influence production orders.
    """

    def __init__(self, sink: HarvestSink) -> None:
        self.sink = sink
        self._states: dict[str, _HarvestState] = {}
        self._by_symbol: dict[str, set[str]] = {}
        self.replays = 0
        self.unavailable = 0
        self.out_of_order_bars = 0

    @staticmethod
    def _side(event: TradingObservationEvent) -> str | None:
        side = (event.side or "").lower()
        if side in {"long", "enter_long", "buy"}:
            return "long"
        if side in {"short", "enter_short", "sell"}:
            return "short"
        return None

    def consume(self, event: TradingObservationEvent) -> None:
        if event.event_type == ShadowEventType.CANDIDATE:
            self._register(event)
        elif event.event_type == ShadowEventType.MARKET_BAR:
            self._bar(event)

    def _register(self, event: TradingObservationEvent) -> None:
        side = self._side(event)
        if not event.candidate_id or event.price is None or event.stop_loss is None or side is None:
            self.unavailable += 1
            return
        entry, stop = float(event.price), float(event.stop_loss)
        if entry <= 0 or (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
            self.unavailable += 1
            return
        if event.candidate_id in self._states:
            return
        state = _HarvestState(candidate=event)
        # Synthetic entry point contains entry only, never entry-bar high/low: this
        # preserves the existing policy's ambiguous-entry-bar exclusion contract.
        state.index.append(event.ts)
        state.rows.append({"open": entry, "high": entry, "low": entry, "close": entry, "atr14": event.atr})
        self._states[event.candidate_id] = state
        self._by_symbol.setdefault(event.symbol.upper(), set()).add(event.candidate_id)

    def _remove(self, state: _HarvestState) -> None:
        cid = state.candidate.candidate_id
        if not cid:
            return
        self._states.pop(cid, None)
        ids = self._by_symbol.get(state.candidate.symbol.upper())
        if ids is not None:
            ids.discard(cid)
            if not ids:
                self._by_symbol.pop(state.candidate.symbol.upper(), None)

    def _bar(self, bar: TradingObservationEvent) -> None:
        for cid in tuple(self._by_symbol.get(bar.symbol.upper(), ())):
            state = self._states.get(cid)
            if state is None:
                continue
            if bar.ts <= state.candidate.ts:
                self.out_of_order_bars += 1
                continue
            if None in (bar.open, bar.high, bar.low, bar.close):
                continue
            state.index.append(bar.ts)
            state.rows.append({"open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
                               "close": float(bar.close), "atr14": bar.atr})
            if (bar.ts - state.candidate.ts).total_seconds() >= 3600:
                self._replay(state)

    def _replay(self, state: _HarvestState) -> None:
        event = state.candidate
        try:
            df = pd.DataFrame(state.rows, index=pd.DatetimeIndex(state.index))
            result = replay_adaptive_harvest(
                df, entry_idx=0, direction=self._side(event) or "",
                entry_price=float(event.price), initial_stop=float(event.stop_loss), max_bars=60,
            )
            if not result or not result.get("replay_valid"):
                self.unavailable += 1
            self.sink(event, result or {"replay_valid": False, "failure_reason": "empty_result"})
            self.replays += 1
        finally:
            self._remove(state)

    def telemetry(self) -> dict[str, int]:
        return {"active": len(self._states), "replays": self.replays,
                "unavailable": self.unavailable, "out_of_order_bars": self.out_of_order_bars}
