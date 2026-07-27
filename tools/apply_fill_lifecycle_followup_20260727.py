"""Follow-up for reviewed fill-lifecycle test/fixture mismatches.

Run after apply_fill_lifecycle_fix_20260727.py from repository root.
Production trading logic is intentionally untouched by this follow-up.
"""
from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


incremental = Path("backend/tests/test_alpaca_incremental_fills.py")
replace_once(
    incremental,
    '    assert [fill.qty for fill in emitted] == [75.0, 25.0]\n    assert broker._processed_fill_qty["order-1"] == 100.0\n',
    '    assert [fill.qty for fill in emitted] == [75.0, 25.0]\n'
    '    # drain_fills only hands the executions to the engine; it must not claim\n'
    '    # durable processing before PortfolioTracker ACKs them. The in-flight\n'
    '    # watermark still advances monotonically and blocks stale duplicates.\n'
    '    assert broker._emitted_fill_qty["order-1"] == 100.0\n'
    '    assert "order-1" not in broker._processed_fill_qty\n',
    "incremental stale-watermark contract",
)

hardened = Path("backend/tests/test_alpaca_hardened_restart_reconciliation.py")
replace_once(
    hardened,
    '    broker._processed_fill_qty = {}\n    broker._processed_fill_notional = {}\n',
    '    broker._processed_fill_qty = {}\n'
    '    broker._processed_fill_notional = {}\n'
    '    broker._emitted_fill_qty = {}\n'
    '    broker._emitted_fill_notional = {}\n',
    "hardened object.__new__ fixture lifecycle state",
)

stabilization = Path("backend/tests/test_prod_stabilization_20260727.py")
replace_once(
    stabilization,
    '    poll_block = broker_src[broker_src.index("async def poll_fills"):broker_src.index("def drain_fills")]\n'
    '    assert "self._processed_fill_qty[oid] = cumulative" not in poll_block\n'
    '    assert "def ack_fill_processed" in broker_src\n',
    '    poll_block = broker_src[broker_src.index("async def poll_fills"):broker_src.index("def ack_fill_processed")]\n'
    '    assert "self._processed_fill_qty[oid] = cumulative" not in poll_block\n'
    '    assert "def ack_fill_processed" in broker_src\n'
    '    assert "def nack_fill_processed" in broker_src\n',
    "stabilization ACK source boundary",
)

print("Aligned fill-lifecycle regression tests and hardened test fixture.")
