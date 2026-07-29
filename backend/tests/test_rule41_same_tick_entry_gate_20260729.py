from pathlib import Path


def _tick_source() -> str:
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    async def _tick(self) -> None:")
    end = src.index("    async def _evaluate_symbol", start)
    return src[start:end]


def test_rule41_runs_before_strategy_entry_permission_is_consumed():
    body = _tick_source()
    verify = body.index("await self.verify_broker_protective_stops(positions)")
    gate = body.index("heartbeat_allows_entries")
    evaluate = body.index("await self._evaluate_symbol", gate)
    assert verify < gate < evaluate


def test_same_tick_entry_gate_intersects_heartbeat_and_rule41_authorities():
    body = _tick_source()
    assert "heartbeat_allows_entries = decision.allow_new_entries if decision is not None else True" in body
    assert "allow_entries = bool(heartbeat_allows_entries and self.allow_new_entries)" in body


def test_rule41_does_not_gain_recovery_authority():
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    async def verify_broker_protective_stops")
    end = src.index("    async def _reconcile_pending_submissions", start)
    verify = src[start:end]
    # Existing contract: #41 may fail-close but never sets entry permission True.
    assert "self.allow_new_entries = False" in verify
    assert "self.allow_new_entries = True" not in verify


def test_heartbeat_remains_recovery_owner_for_transient_rule41_block():
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    start = src.index("    async def _check_system_heartbeat")
    end = src.index("    def _evaluate_market_risk", start)
    heartbeat = src[start:end]
    assert 'self.system_health = "NORMAL"' in heartbeat
    assert "self.allow_new_entries = True" in heartbeat
