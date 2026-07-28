"""Add regression coverage for July-28 runtime/heartbeat architecture findings.

The tests are normal backend/tests pytest tests, so a final `pytest backend/tests`
automatically includes them. This applicator changes tests only.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "backend/tests/test_cr146f_heartbeat.py"


def append_once(text: str) -> None:
    current = TEST.read_text(encoding="utf-8")
    marker = "def test_reconciliation_recovery_reenables_entries_from_current_observations"
    if marker in current:
        print("already: July-28 architecture regression tests")
        return
    TEST.write_text(current.rstrip() + "\n\n\n" + text.strip() + "\n", encoding="utf-8")
    print("applied: July-28 architecture regression tests")


append_once(r'''
def test_reconciliation_recovery_reenables_entries_from_current_observations(client):
    """A recovered SAFE_MODE must not feed the previous allow_new_entries=False back into itself."""
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    from app.portfolio.tracker import _OpenLot

    engine.portfolio._lots["SPY"] = _OpenLot(
        qty=10, avg_price=100.0, entry_ts=datetime.now(UTC), strategy="momentum"
    )
    engine.broker.positions = AsyncMock(return_value=[
        Position(symbol="QQQ", qty=10.0, avg_entry_price=100.0, market_price=100.0)
    ])

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
        assert engine.reconciliation_mismatch is True
        assert engine.allow_new_entries is False

        engine.broker.positions = AsyncMock(return_value=[
            Position(symbol="SPY", qty=10.0, avg_entry_price=100.0, market_price=100.0)
        ])
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()

    assert engine.reconciliation_mismatch is False
    assert engine.system_health == "NORMAL"
    assert engine.allow_new_entries is True
    assert engine.last_safety_decision.allow_new_entries is True


def test_single_reconciliation_mismatch_is_not_latched(client):
    """One broker/local race is observational; only a consecutive confirmation gates entries."""
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    from app.portfolio.tracker import _OpenLot

    engine.portfolio._lots["SPY"] = _OpenLot(
        qty=10, avg_price=100.0, entry_ts=datetime.now(UTC), strategy="momentum"
    )
    engine.broker.positions = AsyncMock(return_value=[
        Position(symbol="QQQ", qty=10.0, avg_entry_price=100.0, market_price=100.0)
    ])

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()

    assert engine._position_reconciliation_mismatch_streak == 1
    assert engine.reconciliation_mismatch is False
    assert engine.system_health == "NORMAL"
    assert engine.allow_new_entries is True


def test_reconciliation_observation_exception_never_declares_reconciled(client):
    """Unavailable broker truth must not be rewritten as a successful reconciliation observation."""
    engine = client.app.state.engine
    setup_healthy_mocks(engine)
    engine.broker.positions = AsyncMock(side_effect=ConnectionError("positions unavailable"))

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))
    finally:
        loop.close()

    assert engine.heartbeat_diagnostics["positions_reconciled"] is False


def test_setup_healthy_mocks_resets_reconciliation_streaks(client):
    """Heartbeat tests must be isolated from prior confirmation streak state."""
    engine = client.app.state.engine
    engine._position_reconciliation_mismatch_streak = 7
    engine._order_reconciliation_mismatch_streak = 9
    setup_healthy_mocks(engine)
    assert engine._position_reconciliation_mismatch_streak == 0
    assert engine._order_reconciliation_mismatch_streak == 0
''')

# Keep the shared test helper state-complete. Without this, the module-scoped app engine
# can leak a confirmation streak from one test into the next and create order-dependent
# failures in the full suite.
text = TEST.read_text(encoding="utf-8")
old = '''    engine.reconciliation_failed = False\n    engine.reconciliation_mismatch = False\n    engine.system_health = "NORMAL"\n'''
new = '''    engine.reconciliation_failed = False\n    engine.reconciliation_mismatch = False\n    engine._position_reconciliation_mismatch_streak = 0\n    engine._order_reconciliation_mismatch_streak = 0\n    engine.system_health = "NORMAL"\n'''
if new not in text:
    if old not in text:
        raise RuntimeError("anchor missing: reset reconciliation streaks in heartbeat test helper")
    TEST.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("applied: reset reconciliation streaks in heartbeat test helper")
else:
    print("already: reset reconciliation streaks in heartbeat test helper")

compile(TEST.read_text(encoding="utf-8"), str(TEST), "exec")
print("Architecture regression tests added; syntax compile passed.")
