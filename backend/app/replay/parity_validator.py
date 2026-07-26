"""Production replay two-pass determinism and forensic divergence validator."""
from __future__ import annotations

from typing import Any
from app.replay.ledger import ReplayLedger


class ProductionParityValidator:
    """Validate replay determinism without weakening any production decision rule."""

    _STAGES = (
        ("scanner_hash", "candidates"),
        ("strategy_hash", "signals"),
        ("ml_hash", "ml_decisions"),
        ("spawn_hash", "spawn_decisions"),
        ("risk_hash", "risk_decisions"),
        ("order_hash", "orders"),
        ("fill_hash", "fills"),
        ("trade_hash", "trades"),
        ("equity_hash", "equity_curve"),
    )

    @staticmethod
    def _first_sequence_diff(a: list[Any], b: list[Any]) -> dict[str, Any] | None:
        limit = min(len(a), len(b))
        for idx in range(limit):
            sa = ReplayLedger._sanitize(a[idx])
            sb = ReplayLedger._sanitize(b[idx])
            if sa != sb:
                return {"index": idx, "pass_a": sa, "pass_b": sb}
        if len(a) != len(b):
            return {
                "index": limit,
                "pass_a": ReplayLedger._sanitize(a[limit]) if limit < len(a) else None,
                "pass_b": ReplayLedger._sanitize(b[limit]) if limit < len(b) else None,
                "count_a": len(a),
                "count_b": len(b),
            }
        return None

    @classmethod
    def validate_passes(cls, pass_a_ledger: ReplayLedger, pass_b_ledger: ReplayLedger) -> dict[str, Any]:
        hashes_a = pass_a_ledger.compute_stage_hashes()
        hashes_b = pass_b_ledger.compute_stage_hashes()
        stage_matches: dict[str, dict[str, Any]] = {}
        first_divergence: dict[str, Any] | None = None

        for stage, attr in cls._STAGES:
            hash_a, hash_b = hashes_a[stage], hashes_b[stage]
            match = hash_a == hash_b
            stage_matches[stage] = {"hash_a": hash_a, "hash_b": hash_b, "match": match}
            if not match and first_divergence is None:
                diff = cls._first_sequence_diff(getattr(pass_a_ledger, attr), getattr(pass_b_ledger, attr))
                first_divergence = {"stage": stage, "collection": attr, **(diff or {})}

        overall_a, overall_b = hashes_a["overall_state_hash"], hashes_b["overall_state_hash"]
        overall_match = overall_a == overall_b
        stage_matches["overall_state_hash"] = {"hash_a": overall_a, "hash_b": overall_b, "match": overall_match}

        if not overall_match and first_divergence is None and pass_a_ledger.production_ticks != pass_b_ledger.production_ticks:
            first_divergence = {
                "stage": "production_ticks",
                "pass_a": pass_a_ledger.production_ticks,
                "pass_b": pass_b_ledger.production_ticks,
            }

        return {
            "overall_match": overall_match,
            "determinism_verdict": "BIT_FOR_BIT_IDENTICAL_PARITY" if overall_match else "DIVERGENCE_DETECTED",
            "stage_matches": stage_matches,
            "first_divergence": first_divergence,
            "metrics_comparison": {
                "pass_a": {
                    "candidate_count": len(pass_a_ledger.candidates),
                    "signal_count": len(pass_a_ledger.signals),
                    "trade_count": len(pass_a_ledger.trades),
                    "order_count": len(pass_a_ledger.orders),
                    "fill_count": len(pass_a_ledger.fills),
                    "final_equity": pass_a_ledger.equity_curve[-1]["equity"] if pass_a_ledger.equity_curve else 100000.0,
                },
                "pass_b": {
                    "candidate_count": len(pass_b_ledger.candidates),
                    "signal_count": len(pass_b_ledger.signals),
                    "trade_count": len(pass_b_ledger.trades),
                    "order_count": len(pass_b_ledger.orders),
                    "fill_count": len(pass_b_ledger.fills),
                    "final_equity": pass_b_ledger.equity_curve[-1]["equity"] if pass_b_ledger.equity_curve else 100000.0,
                },
            },
        }
