"""AlphaDesk 007 entry-loss circuit breaker.

Keeps the existing RiskManager public API while making the already-exposed
strategy cooldown state an actual entry veto after repeated losses in a session.
"""
from __future__ import annotations

from functools import wraps

from app.risk.manager import RiskDecision, RiskManager


def install() -> None:
    if getattr(RiskManager, "_alphadesk_007_entry_guard", False):
        return
    original = RiskManager.is_entry_permitted

    @wraps(original)
    def guarded(self, signal, now=None):
        decision = original(self, signal, now=now)
        if not decision.approved:
            return decision
        strategy = getattr(signal, "strategy", None) or "manual"
        if self.is_strategy_in_cooldown(strategy, now=now):
            count = int(getattr(self, "strategy_consecutive_losses", {}).get(strategy, 0))
            return RiskDecision(
                False,
                f"AlphaDesk 007: {strategy} frozen after {count} consecutive losses in this session",
            )
        return decision

    RiskManager.is_entry_permitted = guarded
    RiskManager._alphadesk_007_entry_guard = True
