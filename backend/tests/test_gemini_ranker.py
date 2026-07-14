from datetime import date

import pytest

from app.ai.gemini_ranker import GeminiShadowRanker, ShadowRank


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


def candidate(symbol, score=70):
    return {"symbol": symbol, "ml_probability": score / 100, "trade_score": score}


@pytest.mark.asyncio
async def test_batches_caps_caches_and_tracks_usage():
    calls = []

    async def transport(payload):
        calls.append(payload)
        return {"rankings": [{"symbol": c["symbol"], "score": 80-i} for i, c in enumerate(payload["candidates"])]}

    clock = Clock()
    ranker = GeminiShadowRanker("key", max_candidates=5, min_interval_sec=300, transport=transport, monotonic=clock)
    items = [candidate(s) for s in ["A", "B", "C", "D", "E", "F"]]
    first = await ranker.rank(items, session=date(2026, 7, 15))
    second = await ranker.rank(items, session=date(2026, 7, 15))
    assert len(first) == 5 and second == first and len(calls) == 1
    assert len(calls[0]["candidates"]) == 5
    assert ranker.telemetry() == {"calls_today": 1, "candidates_ranked": 5, "cache_hits": 1, "failures": 0, "budget_remaining": 9}


@pytest.mark.asyncio
async def test_minimum_interval_and_budget_are_hard_gates():
    clock = Clock()

    async def transport(payload):
        return {"rankings": [{"symbol": payload["candidates"][0]["symbol"], "score": 75}]}

    ranker = GeminiShadowRanker("key", max_calls_per_session=2, min_interval_sec=300, transport=transport, monotonic=clock)
    assert await ranker.rank([candidate("A")], session=date(2026, 7, 15))
    assert await ranker.rank([candidate("B")], session=date(2026, 7, 15)) == ()
    clock.value += 300
    assert await ranker.rank([candidate("B")], session=date(2026, 7, 15))
    clock.value += 300
    assert await ranker.rank([candidate("C")], session=date(2026, 7, 15)) == ()
    assert ranker.telemetry()["budget_remaining"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"rankings": [{"symbol": "A", "score": 101}]}, {"rankings": [{"symbol": "UNKNOWN", "score": 50}]}])
async def test_malformed_or_untrusted_output_fails_open(response):
    async def transport(_):
        return response

    ranker = GeminiShadowRanker("key", transport=transport)
    assert await ranker.rank([candidate("A")], session=date(2026, 7, 15)) == ()
    assert ranker.telemetry()["failures"] == 1


@pytest.mark.asyncio
async def test_transport_failure_never_raises_into_trading_path():
    async def transport(_):
        raise RuntimeError("quota")

    ranker = GeminiShadowRanker("key", transport=transport)
    assert await ranker.rank([candidate("A")], session=date(2026, 7, 15)) == ()
    assert ranker.telemetry()["failures"] == 1


def test_ranker_has_research_only_result_contract():
    fields = set(ShadowRank.__dataclass_fields__)
    assert fields == {"symbol", "score"}
    assert not fields.intersection({"qty", "side", "order", "approved", "execute"})
