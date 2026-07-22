"""Canonical Evidence Layer (#218, #219, #220, #221, #222, #224, #234, #238).

Centralizes authoritative outcome resolution, horizon selection (60m vs session_close),
R-multiple calculation, Null vs 0R breakeven distinction, and strict population identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import pandas as pd
import numpy as np


@dataclass(frozen=True)
class CanonicalEvidence:
    valid: bool
    invalid_reason: str | None
    horizon: str  # e.g., "60m", "session_close"
    realized_r: float | None  # 0.0 is valid 0R breakeven; None means no usable R evidence
    mfe: float | None
    mae: float | None
    regime: str
    regime_source: str  # "VIX" vs "SPY_VOL_PROXY"
    replay_valid: bool
    evidence_version: str = "v1"


class CanonicalOutcomeResolver:
    """Authoritative outcome resolver and population identity manager."""

    @staticmethod
    def compute_canonical_r(
        pnl: float | None,
        entry: float | None,
        stop: float | None,
        mae_pct: float | None = None
    ) -> float | None:
        """Compute canonical R-multiple.
        
        Preserves 0.0 as a valid 0R breakeven trade. Returns None if PnL is missing or invalid.
        """
        if pnl is None or pd.isna(pnl) or not math.isfinite(pnl):
            return None

        # 0.0 PnL is a valid 0R breakeven trade
        if abs(pnl) < 1e-9:
            return 0.0

        if entry is not None and stop is not None and pd.notna(entry) and pd.notna(stop) and entry > 0 and abs(entry - stop) > 0:
            risk_pct = abs(entry - stop) / entry
            dollar_risk = 10000.0 * risk_pct
            if dollar_risk > 0:
                return float(pnl / dollar_risk)

        if mae_pct is not None and pd.notna(mae_pct) and mae_pct > 0:
            dollar_risk_est = 10000.0 * (float(mae_pct) / 100.0)
            if dollar_risk_est > 0:
                return float(pnl / dollar_risk_est)

        return None

    @classmethod
    def resolve(cls, record: dict | pd.Series, horizon: str | None = None) -> CanonicalEvidence:
        """Resolve authoritative outcome from a Shadow Ledger record or payload dict.

        Resolves tickets #218, #219, #220, #224, #234, #238.
        """
        if isinstance(record, pd.Series):
            row_dict = record.to_dict()
        else:
            row_dict = dict(record)

        payload_raw = row_dict.get("payload")
        if isinstance(payload_raw, str) and payload_raw.startswith("{"):
            try:
                payload = json.loads(payload_raw)
            except Exception:
                payload = {}
        elif isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            payload = {}

        strategy_name = str(row_dict.get("strategy") or payload.get("strategy") or "").lower()
        if horizon is None or horizon == "60m":
            from app.strategies.memory import STRATEGY_HORIZON, _DEFAULT_HORIZON
            horizons = STRATEGY_HORIZON.get(strategy_name, _DEFAULT_HORIZON)
            target_horizon = horizons[0] if horizons else "60m"
        else:
            target_horizon = horizon

        # Extract entry, stop, PnL, MAE, MFE
        entry = float(payload.get("price") or payload.get("entry_price") or row_dict.get("entry_price") or 0.0) or None
        stop = float(payload.get("stop_loss") or row_dict.get("stop_loss") or 0.0) or None
        pnl = row_dict.get("simulated_pnl")
        if pnl is None or (isinstance(pnl, float) and pd.isna(pnl)):
            pnl = payload.get("simulated_pnl")

        mae = row_dict.get("mae_pct")
        if mae is None or (isinstance(mae, float) and pd.isna(mae)):
            mae = payload.get("mae_pct")

        mfe = row_dict.get("mfe_pct")
        if mfe is None or (isinstance(mfe, float) and pd.isna(mfe)):
            mfe = payload.get("mfe_pct")

        # Extract regime and regime_source (#221, #222)
        regime = str(payload.get("regime") or row_dict.get("regime") or "RANGE").upper()
        regime_source = str(payload.get("regime_source") or row_dict.get("regime_source") or "VIX").upper()

        # Check replay validity (#230)
        replayed = bool(row_dict.get("replayed", False) or payload.get("replayed", False))
        replay_failed = bool(row_dict.get("replay_failed", False))

        # Calculate Canonical R (#220)
        realized_r = cls.compute_canonical_r(pnl=pnl, entry=entry, stop=stop, mae_pct=mae)
        valid_r = (realized_r is not None)
        
        r_val = row_dict.get("realized_r")
        if r_val is not None:
            try:
                realized_r = float(r_val)
                valid_r = True
            except (ValueError, TypeError):
                pass

        invalid_reason = None
        if not replayed:
            valid_r = False
            invalid_reason = "NOT_REPLAYED"
        elif replay_failed:
            valid_r = False
            invalid_reason = "REPLAY_NOT_VALID"
        elif realized_r is None:
            valid_r = False
            invalid_reason = "MISSING_CANONICAL_R"

        return CanonicalEvidence(
            valid=valid_r,
            invalid_reason=invalid_reason,
            horizon=target_horizon,
            realized_r=realized_r,
            mfe=float(mfe) if mfe is not None and pd.notna(mfe) else None,
            mae=float(mae) if mae is not None and pd.notna(mae) else None,
            regime=regime,
            regime_source=regime_source,
            replay_valid=replayed and not replay_failed,
            evidence_version="v1.0"
        )

    @classmethod
    def filter_population(
        cls,
        records: list[dict | pd.Series],
        regime: str | None = None,
        regime_source: str | None = None,
        gate_id: str | None = None,
        strategy: str | None = None,
        direction: str | None = None,
        fail_on_mixed_sources: bool = True,
    ) -> tuple[list[CanonicalEvidence], dict]:
        """Filter population by strict (regime, regime_source, gate_id, strategy, direction) identity (#221, #222).
        
        Returns (valid_evidence_list, population_metadata).
        Fails closed (returns empty valid evidence) if mixed regime sources (e.g. VIX + SPY_VOL_PROXY)
        are detected and fail_on_mixed_sources is True.
        """
        evidences = [cls.resolve(r) for r in records]
        
        # Collect observed regime sources in the records
        sources_observed = set(e.regime_source for e in evidences if e.valid)

        if fail_on_mixed_sources and len(sources_observed) > 1 and regime_source is None:
            return [], {
                "error": "MIXED_REGIME_SOURCES",
                "sources": list(sources_observed),
                "valid_r_count": 0,
                "total_records": len(records),
            }

        valid_filtered = []
        for r_rec, ev in zip(records, evidences):
            if not ev.valid:
                continue

            r_dict = r_rec.to_dict() if isinstance(r_rec, pd.Series) else dict(r_rec)
            payload_raw = r_dict.get("payload")
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) and payload_raw.startswith("{") else (payload_raw if isinstance(payload_raw, dict) else {})

            rec_strat = str(r_dict.get("strategy") or payload.get("strategy") or "").lower()
            rec_dir = str(r_dict.get("direction") or payload.get("direction") or "").lower()
            rec_gate = str(r_dict.get("rejection_stage") or payload.get("rejection_stage") or "").lower()

            if regime and ev.regime.upper() != regime.upper():
                continue
            if regime_source and ev.regime_source.upper() != regime_source.upper():
                continue
            if strategy and rec_strat != strategy.lower():
                continue
            if direction and rec_dir != direction.lower():
                continue
            if gate_id and rec_gate != gate_id.lower():
                continue

            valid_filtered.append(ev)

        return valid_filtered, {
            "error": None,
            "sources": list(sources_observed),
            "valid_r_count": len(valid_filtered),
            "total_records": len(records),
        }

    @staticmethod
    def get_composite_observation_id(record: dict | pd.Series) -> tuple | None:
        """Extract composite observation identity tuple (#237 Hardening).

        Identity components: (candidate_id, signal_timestamp, strategy, direction).
        """
        r_dict = record.to_dict() if isinstance(record, pd.Series) else dict(record)
        payload_raw = r_dict.get("payload")
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) and payload_raw.startswith("{") else (payload_raw if isinstance(payload_raw, dict) else {})

        cid = str(r_dict.get("candidate_id") or payload.get("candidate_id") or r_dict.get("id") or "").strip()
        if not cid:
            return None

        ts = str(r_dict.get("timestamp") or r_dict.get("ts") or payload.get("timestamp") or payload.get("ts") or "").strip()
        strategy = str(r_dict.get("strategy") or payload.get("strategy") or "").strip().lower()
        direction = str(r_dict.get("direction") or payload.get("direction") or "").strip().lower()

        return (cid, ts, strategy, direction)

    @classmethod
    def calculate_matched_nomination_evidence(
        cls,
        champion_records: list[dict],
        candidate_records: list[dict],
        min_promotion_sample: int = 30,
    ) -> dict:
        """Calculate nomination denominator using matched valid observation pairs (#237).

        Requires composite observation identity matching and matched valid count N >= min_promotion_sample.
        Calculates expectancy gain E = (1/N) * sum(R_challenger,i - R_champion,i).
        """
        champ_map = {}
        for r in champion_records:
            obs_id = cls.get_composite_observation_id(r)
            ev = cls.resolve(r)
            if ev.valid and ev.realized_r is not None and obs_id:
                champ_map[obs_id] = ev

        cand_map = {}
        for r in candidate_records:
            obs_id = cls.get_composite_observation_id(r)
            ev = cls.resolve(r)
            if ev.valid and ev.realized_r is not None and obs_id:
                cand_map[obs_id] = ev

        matched_keys = set(champ_map.keys()) & set(cand_map.keys())
        matched_valid_count = len(matched_keys)
        eligible = matched_valid_count >= min_promotion_sample

        r_diffs = []
        for key in matched_keys:
            r_champ = champ_map[key].realized_r
            r_cand = cand_map[key].realized_r
            if r_champ is not None and r_cand is not None:
                r_diffs.append(r_cand - r_champ)

        expectancy_gain_r = float(np.mean(r_diffs)) if r_diffs else 0.0

        return {
            "matched_valid_count": matched_valid_count,
            "min_promotion_sample": min_promotion_sample,
            "nomination_eligible": eligible,
            "expectancy_gain_r": expectancy_gain_r,
            "reason": "VALID_MATCHED_SAMPLES" if eligible else "INSUFFICIENT_MATCHED_SAMPLES",
            "matched_keys": [f"{k[0]}:{k[1]}:{k[2]}:{k[3]}" for k in matched_keys],
        }
