"""Complete the verified July-28 heartbeat state-machine remediation.

This is intentionally narrow: no strategy, ML/meta threshold, scanner scoring,
position sizing, stop economics, or market-risk tuning.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
TEST = ROOT / "backend/tests/test_cr146f_heartbeat.py"


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"already: {label}")
        return
    if old not in text:
        raise RuntimeError(f"anchor missing: {label}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"applied: {label}")


# RCA: reconciliation SAFE_MODE is correctly confirmed across two heartbeats, but
# allow_new_entries was also used as an input to the next SafetyDecision. On the
# recovery heartbeat it had already been forced False by the previous latch before
# convergence cleared system_health. That made a historical output feed back as a
# current input and left entries disabled even after broker/local truth converged.
# Derive entry permission from CURRENT heartbeat diagnostics instead.
replace_once(
    ENGINE,
    '''        # System-level entry gating: respect manual toggle gating (_force_gated)\n        system_allow_entries = self.allow_new_entries and not getattr(self, "_force_gated", False)\n        \n        is_halted = (self.system_health == "HALTED") or (self.state == EngineState.HALTED) or getattr(self.risk, "hard_halted", False)\n''',
    '''        # System-level entry gating is derived from CURRENT observations, never\n        # from the previous heartbeat's allow_new_entries output. This prevents a\n        # reconciliation-only SAFE_MODE from becoming a hidden permanent latch while\n        # preserving every independent fail-closed monitor and the manual gate.\n        current_health_allows_entries = all((\n            diagnostics.get("broker_connected", False),\n            diagnostics.get("market_data_fresh", False),\n            diagnostics.get("db_connected", False),\n            diagnostics.get("db_write_ok", False),\n            diagnostics.get("model_ready", False),\n            diagnostics.get("scanner_ok", False),\n            diagnostics.get("buying_power_ok", False),\n            diagnostics.get("positions_reconciled", False),\n            diagnostics.get("orders_reconciled", False),\n        ))\n        system_allow_entries = (\n            current_health_allows_entries\n            and self.system_health == "NORMAL"\n            and not getattr(self, "_force_gated", False)\n        )\n        \n        is_halted = (self.system_health == "HALTED") or (self.state == EngineState.HALTED) or getattr(self.risk, "hard_halted", False)\n''',
    "derive heartbeat entry permission from current observations",
)

# The old test encoded the pre-fix immediate-latch contract. The production invariant
# is now: one observation is tolerated, two consecutive observations fail closed.
replace_once(
    TEST,
    '''    loop = asyncio.new_event_loop()\n    try:\n        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))\n    finally:\n        loop.close()\n\n    assert engine.system_health == "SAFE_MODE"\n    assert engine.allow_new_entries is False\n    assert engine.reconciliation_mismatch is True\n''',
    '''    loop = asyncio.new_event_loop()\n    try:\n        # First missing-order observation is treated as a possible broker propagation\n        # race; a second consecutive observation confirms the discrepancy.\n        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))\n        assert engine.system_health == "NORMAL"\n        assert engine.reconciliation_mismatch is False\n        loop.run_until_complete(engine._check_system_heartbeat(datetime.now(UTC)))\n    finally:\n        loop.close()\n\n    assert engine.system_health == "SAFE_MODE"\n    assert engine.allow_new_entries is False\n    assert engine.reconciliation_mismatch is True\n''',
    "align missing-order test with two-heartbeat confirmation contract",
)

compile(ENGINE.read_text(encoding="utf-8"), str(ENGINE), "exec")
compile(TEST.read_text(encoding="utf-8"), str(TEST), "exec")
print("Heartbeat state-machine completion applied; syntax compile passed.")
