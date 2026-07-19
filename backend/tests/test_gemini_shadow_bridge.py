import asyncio
from datetime import date

import pytest

from app.ai.gemini_ranker import GeminiShadowRanker
from app.ai.shadow_bridge import GeminiShadowBridge
from app.core.domain import Regime, Signal, SignalAction


def signal():
    return Signal(symbol="nvda", action=SignalAction.ENTER_LONG, confidence=.9, strategy="breakout", reason="private reason", price=100, stop_loss=98, take_profit=106, ml_probability=.8, regime=Regime.TREND_UP, score=77)


def test_candidate_dto_is_compact_and_has_no_execution_authority():
    dto = GeminiShadowBridge.candidate_dto(signal())
    assert dto == {"symbol": "NVDA", "action": "enter_long", "strategy": "breakout", "ml_probability": .8, "trade_score": 77.0, "regime": "trend_up", "risk_reward": 3.0}
    assert not set(dto).intersection({"qty", "side", "order", "approved", "execute", "price", "stop_loss", "take_profit", "reason", "bars"})


@pytest.mark.asyncio
async def test_network_latency_cannot_block_schedule_caller():
    entered, release = asyncio.Event(), asyncio.Event()
    async def transport(_payload):
        entered.set(); await release.wait(); return {"rankings": [{"symbol": "NVDA", "score": 99}]}
    bridge = GeminiShadowBridge(GeminiShadowRanker("key", transport=transport))
    bridge.schedule([signal()], session=date(2026, 7, 15))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert bridge.task is not None and not bridge.task.done()
    release.set(); await bridge.task


@pytest.mark.asyncio
async def test_shadow_score_has_no_execution_return_channel_and_is_observed():
    observations = []
    async def transport(_payload): return {"rankings": [{"symbol": "NVDA", "score": 0}]}
    bridge = GeminiShadowBridge(GeminiShadowRanker("key", transport=transport), observation_sink=observations.append)
    result = await bridge._run([bridge.candidate_dto(signal())], session=date(2026, 7, 15))
    assert result is None
    assert observations[0]["rankings"] == [{"symbol": "NVDA", "score": 0}]
    assert observations[0]["candidate_symbols"] == ["NVDA"]
    assert len(observations[0]["candidate_fingerprint"]) == 64
    assert observations[0]["telemetry"]["candidates_ranked"] == 1


@pytest.mark.asyncio
async def test_observation_sink_failure_cannot_escape_shadow_task():
    async def transport(_payload): return {"rankings": [{"symbol": "NVDA", "score": 100}]}
    def broken_sink(_observation): raise RuntimeError("storage unavailable")
    bridge = GeminiShadowBridge(GeminiShadowRanker("key", transport=transport), observation_sink=broken_sink)
    result = await bridge._run([bridge.candidate_dto(signal())], session=date(2026, 7, 15))
    assert result is None
    assert bridge.ranker.telemetry()["candidates_ranked"] == 1
