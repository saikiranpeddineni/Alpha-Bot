"""Causal outcome joiner for canonical shadow candidates and subsequent market bars.

The joiner consumes only immutable TradingObservationEvent values. It never fetches
future bars and never has broker/execution authority. Outcomes are keyed by the same
candidate_id emitted by Champion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.research.shadow_event import ShadowEventType, TradingObservationEvent

OutcomeSink = Callable[[TradingObservationEvent, str, dict[str, Any]], None]

HORIZONS_MINUTES: tuple[int, ...] = (1, 5, 15, 30, 60)


@dataclass
class _CandidateState:
    event: TradingObservationEvent
    entry_price: float
    direction: str
    initial_risk: float | None
    mfe: float = 0.0
    mae: float = 0.0
    stopped: bool = False
    completed_horizons: set[int] = field(default_factory=set)


class CanonicalOutcomeJoiner:
    """Joins later market bars to candidate IDs and emits causal MFE/MAE/R evidence."""

    def __init__(self, sink: OutcomeSink) -> None:
        self.sink = sink
        self._active: dict[str, _CandidateState] = {}
        self._by_symbol: dict[str, set[str]] = {}
        self.candidates_seen = 0
        self.bars_seen = 0
        self.outcomes_emitted = 0
        self.duplicate_candidates = 0
        self.out_of_order_bars = 0

    @staticmethod
    def _direction(event: TradingObservationEvent) -> str | None:
        side = (event.side or "").lower()
        if side in {"long", "enter_long", "buy"}:
            return "long"
        if side in {"short", "enter_short", "sell"}:
            return "short"
        return None

    @staticmethod
    def _initial_risk(event: TradingObservationEvent, direction: str, entry: float) -> float | None:
        stop = event.stop_loss
        if stop is None or stop == entry:
            return None
        if direction == "long" and stop >= entry:
            return None
        if direction == "short" and stop <= entry:
            return None
        return abs(entry - stop)

    def consume(self, event: TradingObservationEvent) -> None:
        if event.event_type == ShadowEventType.CANDIDATE:
            self._register_candidate(event)
        elif event.event_type == ShadowEventType.MARKET_BAR:
            self._consume_bar(event)

    def _register_candidate(self, event: TradingObservationEvent) -> None:
        if not event.candidate_id or event.price is None:
            return
        if event.candidate_id in self._active:
            self.duplicate_candidates += 1
            return
        direction = self._direction(event)
        if direction is None:
            return
        state = _CandidateState(
            event=event,
            entry_price=float(event.price),
            direction=direction,
            initial_risk=self._initial_risk(event, direction, float(event.price)),
        )
        self._active[event.candidate_id] = state
        self._by_symbol.setdefault(event.symbol.upper(), set()).add(event.candidate_id)
        self.candidates_seen += 1

    def _consume_bar(self, bar: TradingObservationEvent) -> None:
        self.bars_seen += 1
        symbol = bar.symbol.upper()
        for candidate_id in tuple(self._by_symbol.get(symbol, ())):
            state = self._active.get(candidate_id)
            if state is None:
                continue
            if bar.ts <= state.event.ts:
                self.out_of_order_bars += 1
                continue
            if bar.high is None or bar.low is None or bar.close is None:
                continue
            self._apply_bar(state, bar)

    def _apply_bar(self, state: _CandidateState, bar: TradingObservationEvent) -> None:
        entry = state.entry_price
        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)
        # A missing, equal, or wrong-side stop is unavailable evidence. Never use
        # the raw invalid value to create a synthetic stop event.
        stop = state.event.stop_loss if state.initial_risk is not None else None

        if not state.stopped:
            if state.direction == "long":
                if stop is not None and low <= stop:
                    state.stopped = True
                    state.mae = max(state.mae, entry - float(stop))
                    # Conservative intrabar ordering: do not credit high after stop.
                    if bar.open is not None:
                        state.mfe = max(state.mfe, max(0.0, float(bar.open) - entry))
                else:
                    state.mfe = max(state.mfe, max(0.0, high - entry))
                    state.mae = max(state.mae, max(0.0, entry - low))
            else:
                if stop is not None and high >= stop:
                    state.stopped = True
                    state.mae = max(state.mae, float(stop) - entry)
                    if bar.open is not None:
                        state.mfe = max(state.mfe, max(0.0, entry - float(bar.open)))
                else:
                    state.mfe = max(state.mfe, max(0.0, entry - low))
                    state.mae = max(state.mae, max(0.0, high - entry))

        elapsed_minutes = (bar.ts - state.event.ts).total_seconds() / 60.0
        for horizon in HORIZONS_MINUTES:
            if horizon in state.completed_horizons or elapsed_minutes < horizon:
                continue
            pnl = self._pnl(state, close)
            outcome = self._outcome_payload(state, bar.ts, horizon, pnl)
            state.completed_horizons.add(horizon)
            self.sink(state.event, f"{horizon}m", outcome)
            self.outcomes_emitted += 1

        if len(state.completed_horizons) == len(HORIZONS_MINUTES):
            candidate_id = state.event.candidate_id
            if candidate_id:
                self._active.pop(candidate_id, None)
                symbol_ids = self._by_symbol.get(state.event.symbol.upper())
                if symbol_ids is not None:
                    symbol_ids.discard(candidate_id)
                    if not symbol_ids:
                        self._by_symbol.pop(state.event.symbol.upper(), None)

    @staticmethod
    def _pnl(state: _CandidateState, close: float) -> float:
        if state.stopped and state.initial_risk is not None:
            return -state.initial_risk
        if state.direction == "long":
            return close - state.entry_price
        return state.entry_price - close

    @staticmethod
    def _outcome_payload(state: _CandidateState, bar_ts: datetime, horizon: int, pnl: float) -> dict[str, Any]:
        risk = state.initial_risk
        mfe_r = state.mfe / risk if risk else None
        mae_r = state.mae / risk if risk else None
        realized_r = pnl / risk if risk else None
        return {
            "candidate_id": state.event.candidate_id,
            "horizon": f"{horizon}m",
            "as_of_ts": bar_ts.isoformat(),
            "entry_price": state.entry_price,
            "direction": state.direction,
            "stop_is_synthetic": risk is None,
            "stopped": state.stopped,
            "mfe": round(state.mfe, 6),
            "mae": round(state.mae, 6),
            "mfe_r": round(mfe_r, 6) if mfe_r is not None else None,
            "mae_r": round(mae_r, 6) if mae_r is not None else None,
            "realized_r": round(realized_r, 6) if realized_r is not None else None,
            "fwd_return": round(pnl / state.entry_price, 8) if state.entry_price > 0 else None,
        }

    def telemetry(self) -> dict[str, Any]:
        return {
            "active_candidates": len(self._active),
            "candidates_seen": self.candidates_seen,
            "bars_seen": self.bars_seen,
            "outcomes_emitted": self.outcomes_emitted,
            "duplicate_candidates": self.duplicate_candidates,
            "out_of_order_bars": self.out_of_order_bars,
        }
