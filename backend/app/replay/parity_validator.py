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
        ("risk_hash", "risk_decisions"),
        ("order_hash", "orders"),
        ("fill_hash", "fills"),
        ("trade_hash", "trades"),
        ("equity_hash", "equity_curve"),
    )

    @classmethod
    def _first_value_diff(cls, a: Any, b: Any, path: str = "$") -> dict[str, Any] | None:
        """Return the exact first differing canonical field for forensic diagnosis."""
        if type(a) is not type(b):
            return {"field_path": path, "value_a": a, "value_b": b,
                    "type_a": type(a).__name__, "type_b": type(b).__name__}
        if isinstance(a, dict):
            keys_a, keys_b = set(a), set(b)
            if keys_a != keys_b:
                missing_a = sorted(keys_b - keys_a)
                missing_b = sorted(keys_a - keys_b)
                return {"field_path": path, "value_a": a, "value_b": b,
                        "missing_in_a": missing_a, "missing_in_b": missing_b}
            for key in sorted(keys_a):
                diff = cls._first_value_diff(a[key], b[key], f"{path}.{key}")
                if diff:
                    return diff
            return None
        if isinstance(a, list):
            if len(a) != len(b):
                return {"field_path": path, "value_a": a, "value_b": b,
                        "count_a": len(a), "count_b": len(b)}
            for idx, (va, vb) in enumerate(zip(a, b)):
                diff = cls._first_value_diff(va, vb, f"{path}[{idx}]")
                if diff:
                    return diff
            return None
        if a != b:
            return {"field_path": path, "value_a": a, "value_b": b}
        return None

    @classmethod
    def _first_sequence_diff(cls, a: list[Any], b: list[Any]) -> dict[str, Any] | None:
        limit = min(len(a), len(b))
        for idx in range(limit):
            # Use the exact same canonicalization contract as stage hashing.  This
            # prevents diagnostics from reporting fields deliberately normalized by
            # the replay ledger while keeping every economic decision field strict.
            sa = ReplayLedger._canonicalize_stage_event(a[idx]) if isinstance(a[idx], dict) else ReplayLedger._sanitize(a[idx])
            sb = ReplayLedger._canonicalize_stage_event(b[idx]) if isinstance(b[idx], dict) else ReplayLedger._sanitize(b[idx])
            if sa != sb:
                field_diff = cls._first_value_diff(sa, sb) or {}
                return {"index": idx, "pass_a": sa, "pass_b": sb, **field_diff}
        if len(a) != len(b):
            va = a[limit] if limit < len(a) else None
            vb = b[limit] if limit < len(b) else None
            return {
                "index": limit,
                "pass_a": ReplayLedger._canonicalize_stage_event(va) if isinstance(va, dict) else ReplayLedger._sanitize(va),
                "pass_b": ReplayLedger._canonicalize_stage_event(vb) if isinstance(vb, dict) else ReplayLedger._sanitize(vb),
                "count_a": len(a),
                "count_b": len(b),
                "field_path": "$[sequence_length]",
                "value_a": len(a),
                "value_b": len(b),
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
                "field_path": "$.production_ticks",
                "value_a": pass_a_ledger.production_ticks,
                "value_b": pass_b_ledger.production_ticks,
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
