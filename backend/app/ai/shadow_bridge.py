"""Execution-isolated bridge between qualified trade signals and Gemini shadow research."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from app.ai.gemini_ranker import GeminiShadowRanker
from app.core.domain import Signal
from app.core.logging import applog


class GeminiShadowBridge:
    """Schedules shadow ranking out-of-band and exposes no execution result channel."""

    def __init__(self, ranker: GeminiShadowRanker) -> None:
        self.ranker = ranker
        self.task: asyncio.Task | None = None

    @staticmethod
    def candidate_dto(sig: Signal) -> dict[str, Any]:
        price, stop, target = sig.price, sig.stop_loss, sig.take_profit
        risk_reward = None
        if price is not None and stop is not None and target is not None:
            risk = abs(price - stop)
            if risk > 0:
                risk_reward = round(abs(target - price) / risk, 4)
        return {
            "symbol": sig.symbol.upper(), "action": sig.action.value, "strategy": sig.strategy,
            "ml_probability": sig.ml_probability, "trade_score": sig.score,
            "regime": sig.regime.value if sig.regime is not None else None, "risk_reward": risk_reward,
        }

    def schedule(self, signals: list[Signal], *, session: date) -> None:
        if not self.ranker.api_key or not signals or (self.task is not None and not self.task.done()):
            return
        candidates = [self.candidate_dto(sig) for sig in signals]
        self.task = asyncio.create_task(self._run(candidates, session=session), name="engine-gemini-shadow-ranking")

    async def _run(self, candidates: list[dict[str, Any]], *, session: date) -> None:
        ranks = await self.ranker.rank(candidates, session=session)
        if ranks:
            applog.info("engine.shadow_rank", f"Gemini shadow ranking: {[(rank.symbol, rank.score) for rank in ranks]}")

    async def shutdown(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
