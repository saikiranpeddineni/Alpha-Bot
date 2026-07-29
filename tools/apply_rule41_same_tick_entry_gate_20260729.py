"""Make Rule-41 broker protection authoritative for same-tick entry eligibility.

Heartbeat owns the baseline safety decision and deliberately resets transient health each
cycle. Rule-41 runs later, after authoritative broker positions/orders are fetched. The
strategy loop must therefore intersect the heartbeat decision with Rule-41's current
``allow_new_entries`` result; otherwise an unprotected position can still admit a new
entry during the very tick in which Rule-41 detected it.

This does NOT make Rule-41 own recovery. On the next heartbeat, baseline eligibility is
recomputed normally; a subsequently verified bracket/standalone stop simply leaves the
baseline permission intact.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
engine = ROOT / "backend/app/core/engine.py"
test = ROOT / "backend/tests/test_rule41_same_tick_entry_gate_20260729.py"

src = engine.read_text(encoding="utf-8")
old = '''            # Retrieve active safety decision entries permission\n            decision = getattr(self, "last_safety_decision", None)\n            allow_entries = decision.allow_new_entries if decision is not None else self.allow_new_entries\n'''
new = '''            # Heartbeat establishes baseline eligibility before broker protection is\n            # audited. Rule-41 runs later in this tick and may fail-close entries when\n            # an authoritative broker position lacks a verified stop. Intersect both\n            # authorities so a stale pre-Rule-41 SafetyDecision can never bypass #41.\n            decision = getattr(self, "last_safety_decision", None)\n            heartbeat_allows_entries = decision.allow_new_entries if decision is not None else True\n            allow_entries = bool(heartbeat_allows_entries and self.allow_new_entries)\n'''
if old in src:
    src = src.replace(old, new, 1)
elif new not in src:
    raise SystemExit("strategy entry-gate anchor unknown; refusing unsafe patch")
engine.write_text(src, encoding="utf-8")

test.write_text('''from pathlib import Path\n\n\ndef _tick_source() -> str:\n    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")\n    start = src.index("    async def _tick(self) -> None:")\n    end = src.index("    async def _evaluate_symbol", start)\n    return src[start:end]\n\n\ndef test_rule41_runs_before_strategy_entry_permission_is_consumed():\n    body = _tick_source()\n    verify = body.index("await self.verify_broker_protective_stops(positions)")\n    gate = body.index("heartbeat_allows_entries")\n    evaluate = body.index("await self._evaluate_symbol", gate)\n    assert verify < gate < evaluate\n\n\ndef test_same_tick_entry_gate_intersects_heartbeat_and_rule41_authorities():\n    body = _tick_source()\n    assert "heartbeat_allows_entries = decision.allow_new_entries if decision is not None else True" in body\n    assert "allow_entries = bool(heartbeat_allows_entries and self.allow_new_entries)" in body\n\n\ndef test_rule41_does_not_gain_recovery_authority():\n    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")\n    start = src.index("    async def verify_broker_protective_stops")\n    end = src.index("    async def _reconcile_pending_submissions", start)\n    verify = src[start:end]\n    # Existing contract: #41 may fail-close but never sets entry permission True.\n    assert "self.allow_new_entries = False" in verify\n    assert "self.allow_new_entries = True" not in verify\n\n\ndef test_heartbeat_remains_recovery_owner_for_transient_rule41_block():\n    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")\n    start = src.index("    async def _check_system_heartbeat")\n    end = src.index("    def _evaluate_market_risk", start)\n    heartbeat = src[start:end]\n    assert 'self.system_health = "NORMAL"' in heartbeat\n    assert "self.allow_new_entries = True" in heartbeat\n''', encoding="utf-8")

print("Applied Rule-41 same-tick entry-gate hardening.")
print("Changed: backend/app/core/engine.py")
print("Added: backend/tests/test_rule41_same_tick_entry_gate_20260729.py")
