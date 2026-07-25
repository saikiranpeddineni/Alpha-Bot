"""ProductionParityValidator -- 2-pass determinism & stage-by-stage hash validator.

Compares Pass A vs Pass B across:
  - Scanner candidates
  - Strategy signals
  - ML & Meta probabilities
  - Spawn Commander decisions
  - Risk decisions
  - Order requests
  - Fills & trades
  - Portfolio equity curves

Generates formal Parity Reports and validates bit-for-bit decision matching.
"""
from __future__ import annotations

import json
import hashlib
from typing import Any
from app.replay.ledger import ReplayLedger


class ProductionParityValidator:
    """Validates bit-for-bit determinism across intermediate decision stages."""

    @staticmethod
    def validate_passes(pass_a_ledger: ReplayLedger, pass_b_ledger: ReplayLedger) -> dict[str, Any]:
        hashes_a = pass_a_ledger.compute_stage_hashes()
        hashes_b = pass_b_ledger.compute_stage_hashes()

        stage_matches = {}
        for stage, hash_val_a in hashes_a.items():
            hash_val_b = hashes_b.get(stage)
            stage_matches[stage] = {
                "hash_a": hash_val_a,
                "hash_b": hash_val_b,
                "match": hash_val_a == hash_val_b,
            }

        overall_match = hashes_a["overall_state_hash"] == hashes_b["overall_state_hash"]

        report = {
            "overall_match": overall_match,
            "determinism_verdict": "BIT_FOR_BIT_IDENTICAL_PARITY" if overall_match else "DIVERGENCE_DETECTED",
            "stage_matches": stage_matches,
            "metrics_comparison": {
                "pass_a": {
                    "candidate_count": len(pass_a_ledger.candidates),
                    "signal_count": len(pass_a_ledger.signals),
                    "trade_count": len(pass_a_ledger.trades),
                    "final_equity": pass_a_ledger.equity_curve[-1]["equity"] if pass_a_ledger.equity_curve else 100000.0,
                },
                "pass_b": {
                    "candidate_count": len(pass_b_ledger.candidates),
                    "signal_count": len(pass_b_ledger.signals),
                    "trade_count": len(pass_b_ledger.trades),
                    "final_equity": pass_b_ledger.equity_curve[-1]["equity"] if pass_b_ledger.equity_curve else 100000.0,
                },
            },
        }
        return report
