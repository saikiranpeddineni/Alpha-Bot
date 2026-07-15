"""Budgeted, shadow-only Gemini ranking for already-qualified trade candidates."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable

import httpx

Transport = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ALLOWED_CANDIDATE_FIELDS = frozenset({"symbol", "action", "strategy", "ml_probability", "trade_score", "regime", "relative_strength", "rvol", "vwap_relation", "atr_extension", "risk_reward"})


@dataclass(frozen=True)
class ShadowRank:
    symbol: str
    score: int


class GeminiShadowRanker:
    """Research-only ranker. Results carry no execution authority."""

    def __init__(self, api_key: str, *, max_calls_per_session: int = 10, max_candidates: int = 5,
                 min_interval_sec: float = 300.0, max_payload_bytes: int = 4096,
                 transport: Transport | None = None, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.api_key = api_key.strip()
        self.max_calls_per_session = max_calls_per_session
        self.max_candidates = max_candidates
        self.min_interval_sec = min_interval_sec
        self.max_payload_bytes = max_payload_bytes
        self._transport = transport or self._gemini_request
        self._monotonic = monotonic
        self._session: date | None = None
        self._calls = self._candidates_ranked = self._cache_hits = self._failures = 0
        self._last_call_at: float | None = None
        self._cache: dict[str, tuple[ShadowRank, ...]] = {}

    def roll_session(self, session: date) -> None:
        if session != self._session:
            self._session = session
            self._calls = self._candidates_ranked = self._cache_hits = self._failures = 0
            self._last_call_at = None
            self._cache.clear()

    def telemetry(self) -> dict[str, int]:
        return {"calls_today": self._calls, "candidates_ranked": self._candidates_ranked, "cache_hits": self._cache_hits,
                "failures": self._failures, "budget_remaining": max(0, self.max_calls_per_session - self._calls)}

    async def rank(self, candidates: list[dict[str, Any]], *, session: date) -> tuple[ShadowRank, ...]:
        self.roll_session(session)
        try:
            batch = self._sanitize(candidates[: self.max_candidates])
        except (TypeError, ValueError):
            self._failures += 1
            return ()
        if not self.api_key or not batch or self._calls >= self.max_calls_per_session:
            return ()
        fingerprint = self._fingerprint(batch)
        cached = self._cache.get(fingerprint)
        if cached is not None:
            self._cache_hits += 1
            return cached
        now = self._monotonic()
        if self._last_call_at is not None and now - self._last_call_at < self.min_interval_sec:
            return ()
        payload = {"mode": "shadow_trade_ranking", "instruction": "Rank only supplied candidates. Return JSON rankings only. No trade execution advice.", "candidates": batch}
        if len(json.dumps(payload, separators=(",", ":"), default=str).encode()) > self.max_payload_bytes:
            self._failures += 1
            return ()
        self._calls += 1
        self._last_call_at = now
        try:
            raw = await self._transport(payload)
            ranks = self._validate(raw, {str(c["symbol"]).upper() for c in batch})
        except Exception:
            self._failures += 1
            return ()
        self._candidates_ranked += len(ranks)
        self._cache[fingerprint] = ranks
        return ranks

    @staticmethod
    def _sanitize(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate.get("symbol"):
                raise ValueError("candidate requires symbol")
            out.append({key: candidate[key] for key in ALLOWED_CANDIDATE_FIELDS if key in candidate})
        return out

    @staticmethod
    def _fingerprint(batch: list[dict[str, Any]]) -> str:
        canonical = json.dumps(batch, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _validate(raw: dict[str, Any], allowed: set[str]) -> tuple[ShadowRank, ...]:
        rows = raw.get("rankings")
        if not isinstance(rows, list): raise ValueError("missing rankings")
        seen, out = set(), []
        for row in rows:
            if not isinstance(row, dict): raise ValueError("invalid ranking row")
            symbol, score = str(row.get("symbol", "")).upper(), row.get("score")
            if symbol not in allowed or symbol in seen or isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
                raise ValueError("invalid ranking value")
            seen.add(symbol); out.append(ShadowRank(symbol=symbol, score=score))
        return tuple(sorted(out, key=lambda item: (-item.score, item.symbol)))

    async def _gemini_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.api_key}"
        body = {"contents": [{"parts": [{"text": json.dumps(payload, separators=(",", ":"), default=str)}]}], "generationConfig": {"temperature": 0.0, "maxOutputTokens": 256, "responseMimeType": "application/json"}}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=body); response.raise_for_status(); data = response.json()
        parsed = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        if not isinstance(parsed, dict): raise ValueError("Gemini response must be an object")
        return parsed
