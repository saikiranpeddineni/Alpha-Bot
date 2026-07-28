"""Fix Gemini shadow observability without giving research execution authority."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "backend/app/ai/shadow_bridge.py"
RANKER = ROOT / "backend/app/ai/gemini_ranker.py"


def patch(path: Path, replacements: list[tuple[str, str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new, label in replacements:
        n = text.count(old)
        if n != 1:
            raise RuntimeError(f"{path.name} {label}: expected one match, found {n}")
        text = text.replace(old, new, 1)
    compile(text, str(path), "exec")
    path.write_text(text, encoding="utf-8")

patch(RANKER, [
('''        self._calls = self._candidates_ranked = self._cache_hits = self._failures = 0
        self._last_call_at: float | None = None
''', '''        self._calls = self._candidates_ranked = self._cache_hits = self._failures = 0
        self._attempts = self._skipped_no_key = self._skipped_budget = self._skipped_rate_limit = self._skipped_empty = 0
        self._last_outcome = "NOT_RUN"
        self._last_call_at: float | None = None
''', 'init telemetry'),
('''            self._calls = self._candidates_ranked = self._cache_hits = self._failures = 0
            self._last_call_at = None
''', '''            self._calls = self._candidates_ranked = self._cache_hits = self._failures = 0
            self._attempts = self._skipped_no_key = self._skipped_budget = self._skipped_rate_limit = self._skipped_empty = 0
            self._last_outcome = "NOT_RUN"
            self._last_call_at = None
''', 'roll telemetry'),
('''    def telemetry(self) -> dict[str, int]:
        return {"calls_today": self._calls, "candidates_ranked": self._candidates_ranked, "cache_hits": self._cache_hits,
                "failures": self._failures, "budget_remaining": max(0, self.max_calls_per_session - self._calls)}
''', '''    def telemetry(self) -> dict[str, Any]:
        return {"enabled": bool(self.api_key), "attempts_today": self._attempts, "calls_today": self._calls,
                "candidates_ranked": self._candidates_ranked, "cache_hits": self._cache_hits,
                "failures": self._failures, "skipped_no_key": self._skipped_no_key,
                "skipped_budget": self._skipped_budget, "skipped_rate_limit": self._skipped_rate_limit,
                "skipped_empty": self._skipped_empty, "last_outcome": self._last_outcome,
                "budget_remaining": max(0, self.max_calls_per_session - self._calls)}
''', 'telemetry schema'),
('''        if not self.roll_session(session):
            return ()
        try:
''', '''        if not self.roll_session(session):
            self._last_outcome = "STALE_SESSION"
            return ()
        self._attempts += 1
        try:
''', 'attempt count'),
('''        except (TypeError, ValueError):
            self._failures += 1
            return ()
        if not self.api_key or not batch or self._calls >= self.max_calls_per_session:
            return ()
''', '''        except (TypeError, ValueError):
            self._failures += 1
            self._last_outcome = "INVALID_CANDIDATE"
            return ()
        if not self.api_key:
            self._skipped_no_key += 1
            self._last_outcome = "SKIPPED_NO_API_KEY"
            return ()
        if not batch:
            self._skipped_empty += 1
            self._last_outcome = "SKIPPED_EMPTY_BATCH"
            return ()
        if self._calls >= self.max_calls_per_session:
            self._skipped_budget += 1
            self._last_outcome = "SKIPPED_BUDGET"
            return ()
''', 'skip reasons'),
('''        if cached is not None:
            self._cache_hits += 1
            return cached
''', '''        if cached is not None:
            self._cache_hits += 1
            self._last_outcome = "CACHE_HIT"
            return cached
''', 'cache outcome'),
('''        if self._last_call_at is not None and now - self._last_call_at < self.min_interval_sec:
            return ()
''', '''        if self._last_call_at is not None and now - self._last_call_at < self.min_interval_sec:
            self._skipped_rate_limit += 1
            self._last_outcome = "SKIPPED_RATE_LIMIT"
            return ()
''', 'rate outcome'),
('''        except Exception:
            self._failures += 1
            return ()
        self._candidates_ranked += len(ranks)
''', '''        except Exception:
            self._failures += 1
            self._last_outcome = "REQUEST_FAILED"
            return ()
        self._candidates_ranked += len(ranks)
        self._last_outcome = "RANKED" if ranks else "EMPTY_RESPONSE"
''', 'request outcome'),
])

patch(BRIDGE, [
('''        self.dropped_pending = 0
        self.completed_batches = 0
''', '''        self.dropped_pending = 0
        self.completed_batches = 0
        self.attempted_batches = 0
        self.skipped_no_key = 0
''', 'bridge counters'),
('''    def _enqueue(self, candidates: list[dict[str, Any]], session: date) -> None:
        if not candidates or not self.ranker.api_key:
            return
''', '''    def _enqueue(self, candidates: list[dict[str, Any]], session: date) -> None:
        if not candidates:
            return
        # Missing credentials are an observable shadow state, not a silent no-op.
        # Record the attempted batch synchronously without creating async work.
        if not self.ranker.api_key:
            self.attempted_batches += 1
            self.skipped_no_key += 1
            self.ranker.roll_session(session)
            self.ranker._attempts += 1
            self.ranker._skipped_no_key += 1
            self.ranker._last_outcome = "SKIPPED_NO_API_KEY"
            self._emit_observation(candidates, session=session, ranks=())
            return
''', 'missing key observability'),
('''            try:
                await self._run(candidates, session=session)
                self.completed_batches += 1
''', '''            try:
                self.attempted_batches += 1
                await self._run(candidates, session=session)
                self.completed_batches += 1
''', 'attempted batches'),
('''    async def _run(self, candidates: list[dict[str, Any]], *, session: date) -> None:
        fingerprint = self.ranker.candidate_fingerprint(candidates)
        ranks = await self.ranker.rank(candidates, session=session)
        observation = {"session": session.isoformat(), "candidate_fingerprint": fingerprint,
''', '''    async def _run(self, candidates: list[dict[str, Any]], *, session: date) -> None:
        ranks = await self.ranker.rank(candidates, session=session)
        self._emit_observation(candidates, session=session, ranks=ranks)
        if ranks:
            applog.info("engine.shadow_rank", f"Gemini shadow ranking: {[(rank.symbol, rank.score) for rank in ranks]}")

    def _emit_observation(self, candidates: list[dict[str, Any]], *, session: date, ranks) -> None:
        fingerprint = self.ranker.candidate_fingerprint(candidates)
        observation = {"session": session.isoformat(), "candidate_fingerprint": fingerprint,
''', 'split observation emitter'),
('''                       "bridge": {"pending": len(self._pending), "dropped_pending": self.dropped_pending,
                                   "completed_batches": self.completed_batches},
''', '''                       "bridge": {"pending": len(self._pending), "dropped_pending": self.dropped_pending,
                                   "attempted_batches": self.attempted_batches,
                                   "completed_batches": self.completed_batches,
                                   "skipped_no_key": self.skipped_no_key},
''', 'bridge observation telemetry'),
('''        if ranks:
            applog.info("engine.shadow_rank", f"Gemini shadow ranking: {[(rank.symbol, rank.score) for rank in ranks]}")

    async def shutdown''', '''    async def shutdown''', 'remove duplicate rank log'),
])

print("Applied Gemini shadow observability fixes; syntax compile passed.")
