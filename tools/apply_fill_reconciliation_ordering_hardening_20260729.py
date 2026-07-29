"""Prevent broker reconciliation from racing the durable fill consumer.

Broker position truth may advance before poll_fills/drain_fills exposes the same
execution.  Rebuilding PortfolioTracker lots from that newer broker snapshot and
then consuming the delayed fill would double-apply quantity/P&L.

Invariant: reconciliation may reconstruct lots only when the fill queue has been
polled and drained for the cycle.  During recovery, poll/drain fills first, then
fetch authoritative positions and rebuild once.

Run from repository root:
    python tools/apply_fill_reconciliation_ordering_hardening_20260729.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''            account = await self.broker.account()\n            positions = await self.broker.positions()\n            if getattr(self, "reconciliation_failed", False):\n                applog.info("engine", "Reconciliation state was uncertain. Re-attempting position reconciliation...")\n                self._reconcile_positions(positions)\n            if positions and not getattr(self, "reconciliation_failed", False):'''
new = '''            account = await self.broker.account()\n            positions = await self.broker.positions()\n            if getattr(self, "reconciliation_failed", False):\n                # Never rebuild local lots over an execution that broker position\n                # truth already reflects but the durable fill consumer has not yet\n                # applied.  Poll/drain once more at the recovery boundary, then\n                # refresh broker positions and reconstruct from that settled view.\n                applog.info("engine", "Reconciliation state was uncertain. Draining fills before position reconstruction...")\n                if not isinstance(self.broker, PaperBroker) and hasattr(self.broker, "poll_fills"):\n                    await self.broker.poll_fills()\n                await self._drain_fills(now)\n                positions = await self.broker.positions()\n                self._reconcile_positions(positions)\n            if positions and not getattr(self, "reconciliation_failed", False):'''
if old not in src:
    raise SystemExit("tick reconciliation anchor not found; refusing unsafe patch")
src = src.replace(old, new, 1)

old = '''    def _reconcile_positions(self, positions: list[Position]) -> None:\n        """Reconcile and reconstruct database portfolio lot states from broker positions."""\n        self.portfolio._lots.clear()'''
new = '''    def _reconcile_positions(self, positions: list[Position]) -> None:\n        """Reconcile lots from broker truth after the cycle's fill queue is drained.\n\n        This method is intentionally synchronous and must not poll the broker. Callers\n        are responsible for draining fills first; otherwise a delayed fill can be\n        applied again on top of the reconstructed broker quantity.\n        """\n        self.portfolio._lots.clear()'''
if old not in src:
    raise SystemExit("reconcile method anchor not found")
src = src.replace(old, new, 1)

if src == original:
    raise SystemExit("no changes applied")
ENGINE.write_text(src, encoding="utf-8")
print("Applied fill/reconciliation ordering hardening to backend/app/core/engine.py")
