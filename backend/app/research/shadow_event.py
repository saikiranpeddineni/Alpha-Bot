"""Canonical immutable production-to-shadow observation contract.

This module intentionally has no broker, portfolio, risk-manager, or execution imports.
Research consumers receive value snapshots only; they never receive mutable production objects.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHADOW_EVENT_SCHEMA_VERSION = 1


class ShadowEventType(StrEnum):
    CANDIDATE = "CANDIDATE"
    MARKET_BAR = "MARKET_BAR"


class EvidenceProvenance(BaseModel):
    """Versions needed to reproduce a shadow decision without execution authority."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    code_commit_sha: str | None = None
    config_version: str | None = None
    model_manifest_sha: str | None = None
    calibration_version: str | None = None
    calibration_cutoff_utc: datetime | None = None
    calibration_sample_size: int | None = Field(default=None, ge=0)


class TradingObservationEvent(BaseModel):
    """Immutable, causally timestamped event mirrored from Champion into research."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=SHADOW_EVENT_SCHEMA_VERSION, ge=1)
    event_type: ShadowEventType
    event_id: str
    candidate_id: str | None = None
    ts: datetime
    decision_bar_timestamp_utc: datetime | None = None
    symbol: str
    side: str | None = None
    strategy: str | None = None
    price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    atr: float | None = None
    rsi: float | None = None
    features: Mapping[str, float | int | None] = Field(default_factory=dict)
    discovery_source: str | None = None
    scanner_score: float | None = None
    scanner_auc: float | None = None
    ml_probability: float | None = None
    ml_decision: str | None = None
    ml_model_source: str | None = None
    ml_model_status: str | None = None
    ml_model_auc: float | None = None
    meta_probability: float | None = None
    meta_decision: str | None = None
    trade_score: float | None = None
    trade_score_decision: str | None = None
    portfolio_decision: str | None = None
    risk_decision: str | None = None
    rejection_reason: str | None = None
    champion_final_decision: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    regime: str | None = None
    vix: float | None = None
    vix_status: str | None = None
    equity: float | None = None
    cash: float | None = None
    buying_power: float | None = None
    calibrated_expected_net_dollars: float | None = None
    expected_holding_mins: float | None = None
    expectancy_confidence: float | None = None
    expectancy_source: str | None = None
    provenance: EvidenceProvenance = Field(default_factory=EvidenceProvenance)

    @model_validator(mode="after")
    def validate_causal_identity(self) -> "TradingObservationEvent":
        if self.ts.tzinfo is None:
            raise ValueError("shadow event ts must be timezone-aware")
        if self.decision_bar_timestamp_utc is not None:
            if self.decision_bar_timestamp_utc.tzinfo is None:
                raise ValueError("decision_bar_timestamp_utc must be timezone-aware")
            if self.decision_bar_timestamp_utc > self.ts:
                raise ValueError("decision bar cannot be later than shadow event")
        if self.event_type == ShadowEventType.CANDIDATE and not self.candidate_id:
            raise ValueError("candidate events require candidate_id")
        if self.event_type == ShadowEventType.MARKET_BAR and self.candidate_id is not None:
            raise ValueError("market-bar events must not invent candidate_id")
        if self.calibrated_expected_net_dollars is not None and self.expectancy_source != "CALIBRATED":
            raise ValueError("calibrated expectancy value must be explicitly tagged CALIBRATED")
        if self.expectancy_source == "CALIBRATED":
            if self.calibrated_expected_net_dollars is None:
                raise ValueError("CALIBRATED expectancy requires calibrated_expected_net_dollars")
            cutoff = self.provenance.calibration_cutoff_utc
            if cutoff is None or cutoff.tzinfo is None:
                raise ValueError("CALIBRATED expectancy requires timezone-aware calibration cutoff")
            if cutoff >= self.ts:
                raise ValueError("calibration cutoff must be strictly before candidate timestamp")
            if not self.provenance.calibration_version:
                raise ValueError("CALIBRATED expectancy requires calibration version")
            if self.provenance.calibration_sample_size is None:
                raise ValueError("CALIBRATED expectancy requires calibration sample size")
            if self.expectancy_confidence is None:
                raise ValueError("CALIBRATED expectancy requires confidence")
        return self

    def legacy_spawn_payload(self) -> dict[str, Any]:
        """Compatibility adapter for Spawn. Missing evidence remains ``None``."""
        if self.event_type == ShadowEventType.MARKET_BAR:
            return {"observation_type": "MARKET_BAR", "schema_version": self.schema_version, "event_id": self.event_id,
                    "symbol": self.symbol.upper(), "open": self.open, "high": self.high, "low": self.low,
                    "close": self.close, "ts": self.ts}
        return {
            "observation_type": "CANDIDATE", "schema_version": self.schema_version, "event_id": self.event_id,
            "candidate_id": self.candidate_id, "symbol": self.symbol.upper(), "strategy": self.strategy,
            "direction": self.side, "side": self.side, "price": self.price, "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "atr": self.atr, "rsi": self.rsi, "features": dict(self.features),
            "stop_loss": self.stop_loss, "take_profit": self.take_profit, "ts": self.ts,
            "decision_bar_timestamp_utc": self.decision_bar_timestamp_utc,
            "calibrated_expected_net_dollars": self.calibrated_expected_net_dollars,
            "expected_holding_mins": self.expected_holding_mins, "expectancy_confidence": self.expectancy_confidence,
            "expectancy_source": self.expectancy_source, "provenance": self.provenance.model_dump(mode="json"),
            "champion_candidate": {"id": self.candidate_id, "ml_probability": self.ml_probability,
                                   "meta_probability": self.meta_probability, "trade_score": self.trade_score,
                                   "final_decision": self.champion_final_decision},
        }

    def immutable_features(self) -> Mapping[str, float | int | None]:
        return MappingProxyType(dict(self.features))
