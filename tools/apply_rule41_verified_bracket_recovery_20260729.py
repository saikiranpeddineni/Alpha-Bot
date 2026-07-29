"""Surgical Rule-41 recovery hardening for verified broker bracket stops.

The current engine already recognizes explicit Alpaca bracket stop children as valid
protection.  This patch fixes the sticky global DEGRADED/entry-block state after all
positions become broker-verified again, without trusting bracket-parent metadata.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
engine = ROOT / "backend/app/core/engine.py"
test = ROOT / "backend/tests/test_rule41_verified_bracket_recovery_20260729.py"

src = engine.read_text(encoding="utf-8")

old_exc = '''            self.allow_new_entries = False\n            if self.system_health not in ("HALTED", "SAFE_MODE"):\n                self.system_health = "DEGRADED"\n            return {p.symbol: False for p in active_positions}\n'''
new_exc = '''            self._rule41_entry_blocked = True\n            self.allow_new_entries = False\n            if self.system_health not in ("HALTED", "SAFE_MODE"):\n                self.system_health = "DEGRADED"\n            return {p.symbol: False for p in active_positions}\n'''
if old_exc in src:
    src = src.replace(old_exc, new_exc, 1)
elif new_exc not in src:
    raise SystemExit("Rule-41 broker-query failure anchor unknown; refusing unsafe patch")

old_unprotected = '''        if unprotected:\n            self.allow_new_entries = False\n            current_health = getattr(self, "system_health", "NORMAL")\n'''
new_unprotected = '''        if unprotected:\n            self._rule41_entry_blocked = True\n            self.allow_new_entries = False\n            current_health = getattr(self, "system_health", "NORMAL")\n'''
if old_unprotected in src:
    src = src.replace(old_unprotected, new_unprotected, 1)
elif new_unprotected not in src:
    raise SystemExit("Rule-41 unprotected anchor unknown; refusing unsafe patch")

old_tail = '''        if not all(protection_status.values()):\n            self.allow_new_entries = False\n            if self.system_health not in ("HALTED", "SAFE_MODE"):\n                self.system_health = "DEGRADED"\n\n        return protection_status\n'''
new_tail = '''        if not all(protection_status.values()):\n            self._rule41_entry_blocked = True\n            self.allow_new_entries = False\n            if self.system_health not in ("HALTED", "SAFE_MODE"):\n                self.system_health = "DEGRADED"\n        elif getattr(self, "_rule41_entry_blocked", False):\n            # Rule-41 owns only the block it created.  Once every authoritative broker\n            # position has a real active stop (standalone or explicit bracket child),\n            # release that stale block.  Preserve independent safety-gate authority.\n            self._rule41_entry_blocked = False\n            decision = getattr(self, "last_safety_decision", None)\n            safety_allows = bool(getattr(decision, "allow_new_entries", True))\n            self.allow_new_entries = safety_allows\n            if safety_allows and self.system_health == "DEGRADED":\n                self.system_health = "NORMAL"\n            applog.info(\n                "engine.protection",\n                "#41: All broker positions have verified protective stops; cleared Rule-41 entry block.",\n            )\n\n        return protection_status\n'''
if old_tail in src:
    src = src.replace(old_tail, new_tail, 1)
elif new_tail not in src:
    raise SystemExit("Rule-41 completion anchor unknown; refusing unsafe patch")

engine.write_text(src, encoding="utf-8")

test.write_text('''from pathlib import Path\n\n\ndef _engine_source() -> str:\n    return (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")\n\n\ndef _valid_stop_source() -> str:\n    src = _engine_source()\n    start = src.index("    def _is_valid_protective_stop")\n    end = src.index("    async def verify_broker_protective_stops", start)\n    return src[start:end]\n\n\ndef _verify_source() -> str:\n    src = _engine_source()\n    start = src.index("    async def verify_broker_protective_stops")\n    end = src.index("    async def _reconcile_pending_submissions", start)\n    return src[start:end]\n\n\ndef test_explicit_bracket_stop_child_is_valid_protection_but_parent_metadata_is_not():\n    body = _valid_stop_source()\n    assert 'leg in {"stop", "stop_loss"}' in body\n    assert "OrderType.STOP" in body\n    assert "OrderType.STOP_LIMIT" in body\n    assert "parent class or" in body.lower()\n    assert "if not (is_standalone_stop or is_stop_leg):" in body\n\n\ndef test_rule41_tracks_ownership_of_its_entry_block():\n    body = _verify_source()\n    assert "self._rule41_entry_blocked = True" in body\n    assert 'getattr(self, "_rule41_entry_blocked", False)' in body\n    assert "self._rule41_entry_blocked = False" in body\n\n\ndef test_verified_protection_releases_only_rule41_block_and_respects_safety_gate():\n    body = _verify_source()\n    recovery = body[body.index('elif getattr(self, "_rule41_entry_blocked", False):'):]\n    assert 'decision = getattr(self, "last_safety_decision", None)' in recovery\n    assert 'getattr(decision, "allow_new_entries", True)' in recovery\n    assert "self.allow_new_entries = safety_allows" in recovery\n    assert 'if safety_allows and self.system_health == "DEGRADED":' in recovery\n\n\ndef test_partial_bracket_fail_closed_logic_is_preserved():\n    body = _verify_source()\n    assert "partial_bracket_entries" in body\n    assert "AWAITING_CANCEL_CONFIRMATION" in body\n    assert "requested cancellation of remaining" in body\n''', encoding="utf-8")

print("Applied Rule-41 verified-bracket recovery hardening.")
print("Changed: backend/app/core/engine.py")
print("Added: backend/tests/test_rule41_verified_bracket_recovery_20260729.py")
