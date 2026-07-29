from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .engine_adapter import PISEvaluation


def evaluation_payload(evaluation: PISEvaluation) -> dict[str, Any]:
    """Stable audit payload for every PIS position-management decision."""
    decision = evaluation.decision
    snapshot = decision.snapshot
    protection = evaluation.intent.protection
    return {
        "policy_version": decision.policy_version,
        "symbol": snapshot.symbol,
        "side": snapshot.side,
        "timestamp": snapshot.timestamp.isoformat(),
        "action": decision.action.value,
        "confidence": decision.confidence,
        "reason_codes": list(decision.reason_codes),
        "lifecycle": snapshot.lifecycle.value,
        "thesis_state": snapshot.thesis_state.value,
        "health": snapshot.health,
        "thesis_drift": snapshot.thesis_drift,
        "current_r": snapshot.current_r,
        "peak_r": snapshot.peak_r,
        "trough_r": snapshot.trough_r,
        "giveback_r": snapshot.giveback_r,
        "components": dict(snapshot.components),
        "close_position": evaluation.intent.close_position,
        "protection": asdict(protection) if protection is not None else None,
        "suppress_legacy_exit": evaluation.intent.suppress_legacy_exit,
    }
