from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadinessInputs:
    engine_cutover_complete: bool
    legacy_discretionary_exits_removed: bool
    hard_safety_controls_intact: bool
    restart_recovery_verified: bool
    replay_verified: bool
    shadow_verified: bool
    promotion_gate_passed: bool
    stale_data_fail_safe_verified: bool
    duplicate_close_verified: bool


@dataclass(frozen=True)
class ReadinessDecision:
    ready: bool
    blockers: tuple[str, ...]


def production_readiness(inputs: ReadinessInputs) -> ReadinessDecision:
    blockers: list[str] = []
    for name, value in vars(inputs).items():
        if not value:
            blockers.append(f"PIS_NOT_READY_{name.upper()}")
    return ReadinessDecision(ready=not blockers, blockers=tuple(blockers))
