"""Execution-isolated bridge between canonical trade observations and Gemini shadow research."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import date
from typing import Any, Callable

from app.ai.gemini_ranker import GeminiShadowRanker
from app.core.domain import Signal
from app.core.logging import applog
from app.research.shadow_event import ShadowEventType, TradingObservationEvent

ObservationSink = Callable[[dict[str, Any]], None]


class GeminiShadowBridge:
    """Queued shadow ranking with no execution result channel."""

    def __init__(self, ranker: GeminiShadowRanker, *, observation_sink: ObservationSink | None = None,
                 max_pending: int = 500) -> None:
        self.ranker = ranker
        self.observation_sink = observation_sink
        self.task: asyncio.Task | None = None
        self._pending: deque[tuple[list[dict[str, Any]], date]] = deque(maxlen=max(1, max_pending))
        self.dropped_pending = 0
        self.completed_batches = 0

    @staticmethod
    def candidate_dto(sig: Signal) -> dict[str, Any]:
        price, stop, target = sig.price, sig.stop_loss, sig.take_profit
        risk_reward = None
        if price is not None and stop is not None and target is not None:
            risk = abs(price - stop)
            if risk > 0:
                risk_reward = round(abs(target - price) / risk, 4)
        return {"symbol": sig.symbol.upper(), "action": sig.action.value, "strategy": sig.strategy,
                "ml_probability": sig.ml_probability, "trade_score": sig.score,
                "regime": sig.regime.value if sig.regime is not None else None, "risk_reward": risk_reward}

    @staticmethod
    def event_candidate_dto(event: TradingObservationEvent) -> dict[str, Any]:
        risk_reward = None
        if event.price is not None and event.stop_loss is not None and event.take_profit is not None:
            risk = abs(event.price - event.stop_loss)
            if risk > 0:
                risk_reward = round(abs(event.take_profit - event.price) / risk, 4)
        action = "enter_long" if event.side == "long" else "enter_short" if event.side == "short" else event.side
        return {"symbol": event.symbol.upper(), "action": action, "strategy": event.strategy,
                "ml_probability": event.ml_probability, "trade_score": event.trade_score,
                "regime": event.regime, "risk_reward": risk_reward, "candidate_id": event.candidate_id}

    def _enqueue(self, candidates: list[dict[str, Any]], session: date) -> None:
        if not candidates or not self.ranker.api_key:
            return
        if len(self._pending) == self._pending.maxlen:
            # deque(maxlen) would silently evict; count it explicitly for evidence coverage.
            self._pending.popleft()
            self.dropped_pending += 1
        self._pending.append((candidates, session))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain(), name="engine-gemini-shadow-ranking")

    def consume(self, event: TradingObservationEvent) -> None:
        if event.event_type != ShadowEventType.CANDIDATE:
            return
        if event.champion_final_decision not in ("accepted", "bypassed", "pipeline_approved"):
            return
        self._enqueue([self.event_candidate_dto(event)], event.ts.date())

    def schedule(self, signals: list[Signal], *, session: date) -> None:
        self._enqueue([self.candidate_dto(sig) for sig in signals], session)

    async def _drain(self) -> None:
        while self._pending:
            candidates, session = self._pending.popleft()
            try:
                await self._run(candidates, session=session)
                self.completed_batches += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One external ranking failure must not discard later queued observations.
                applog.warning("engine.shadow_rank", f"Gemini shadow batch failed open: {exc}")

    async def _run(self, candidates: list[dict[str, Any]], *, session: date) -> None:
        fingerprint = self.ranker.candidate_fingerprint(candidates)
        ranks = await self.ranker.rank(candidates, session=session)
        observation = {"session": session.isoformat(), "candidate_fingerprint": fingerprint,
                       "candidate_symbols": [str(c["symbol"]).upper() for c in candidates[: self.ranker.max_candidates]],
                       "candidate_ids": [c.get("candidate_id") for c in candidates[: self.ranker.max_candidates] if c.get("candidate_id")],
                       "rankings": [{"symbol": rank.symbol, "score": rank.score} for rank in ranks],
                       "bridge": {"pending": len(self._pending), "dropped_pending": self.dropped_pending,
                                  "completed_batches": self.completed_batches},
                       "telemetry": self.ranker.telemetry()}
        if self.observation_sink is not None:
            try:
                self.observation_sink(observation)
            except Exception as exc:
                applog.warning("engine.shadow_rank", f"Shadow observation sink failed: {exc}")
        if ranks:
            applog.info("engine.shadow_rank", f"Gemini shadow ranking: {[(rank.symbol, rank.score) for rank in ranks]}")

    async def shutdown(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self._pending.clear()
