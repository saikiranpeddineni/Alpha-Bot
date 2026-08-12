"""Context-aware entry intelligence for Champion candidates.

The purpose of this module is not to suppress trading. It is to distinguish
high-conviction opportunities from repeated, poorly aligned attempts by using
multiple independent pieces of market evidence and recent same-session outcomes.

Evidence is weighted by information value rather than counted equally. A fresh
material catalyst, a confirmed market-direction conflict, and repeated losses on
the same thesis carry more weight than a small EMA disagreement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction

SHORT_TERM_SLOPE_R = 0.10
EXTENSION_R = 0.10
NEWS_FRESH_SECONDS = 30 * 60
NEWS_MATERIAL_CONFIDENCE = 0.55
REENTRY_LOOKBACK_MINUTES = 60
REENTRY_LOSS_BLOCK_COUNT = 2
REENTRY_HARD_BLOCK_COUNT = 3
MIN_CONVICTION = 0.65
HARD_CONVICTION = 0.54


@dataclass(frozen=True)
class EntryContextAssessment:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    evidence: dict[str, Any] = field(default_factory=dict)


def _normalized_short_term_move(df: pd.DataFrame, bars: int = 4) -> float | None:
    if len(df) < bars + 1 or "close" not in df.columns or "atr14" not in df.columns:
        return None
    atr = float(df["atr14"].iloc[-1])
    if atr <= 0:
        return None
    closes = df["close"].astype(float)
    return (float(closes.iloc[-1]) - float(closes.iloc[-(bars + 1)])) / max(atr, 1e-9)


def _reversal_confirmed(sig: Signal, df: pd.DataFrame) -> bool:
    if len(df) < 8 or "atr14" not in df.columns or "close" not in df.columns:
        return False
    atr = float(df["atr14"].iloc[-1])
    if atr <= 0:
        return False
    closes = df["close"].astype(float)
    last = float(closes.iloc[-1])
    slope_r = (last - float(closes.iloc[-5])) / max(atr, 1e-9)
    prev_high = float(df["high"].iloc[-7:-1].max()) if "high" in df.columns else float(closes.iloc[-6:-1].max())
    prev_low = float(df["low"].iloc[-7:-1].min()) if "low" in df.columns else float(closes.iloc[-6:-1].min())
    if sig.side == "long":
        return slope_r >= SHORT_TERM_SLOPE_R and last > prev_high
    return slope_r <= -SHORT_TERM_SLOPE_R and last < prev_low


def _market_direction_conflict(sig: Signal, df: pd.DataFrame, regime: Regime | str | None) -> tuple[float, list[str], dict[str, Any]]:
    reasons: list[str] = []
    evidence: dict[str, Any] = {}
    regime_name = str(getattr(regime, "value", regime) or "").lower()
    mover = getattr(sig, "mover_bias", None)
    rs = getattr(sig, "rs_score", None)
    slope_r = _normalized_short_term_move(df)
    reversal = _reversal_confirmed(sig, df)
    conflict_weight = 0.0

    if sig.side == "long":
        direction_ok = mover == "gainer" or (rs is not None and rs >= 0.55) or (slope_r is not None and slope_r >= SHORT_TERM_SLOPE_R)
        if mover == "loser":
            conflict_weight += 0.25
            reasons.append("directional_context_conflict: loser_mover")
        if rs is not None and rs < 0.45:
            conflict_weight += 0.20
            reasons.append("directional_context_conflict: weak_relative_strength")
        if regime_name == Regime.TREND_DOWN.value and not reversal:
            conflict_weight += 0.25
            reasons.append("regime_conflict: long_against_trend_down")
        if slope_r is not None and slope_r <= -SHORT_TERM_SLOPE_R and not reversal:
            conflict_weight += 0.18
            reasons.append("microstructure_conflict: short_term_downside_confirmation")
    else:
        direction_ok = mover == "loser" or (rs is not None and rs <= 0.45) or (slope_r is not None and slope_r <= -SHORT_TERM_SLOPE_R)
        if mover == "gainer":
            conflict_weight += 0.25
            reasons.append("directional_context_conflict: gainer_mover")
        if rs is not None and rs > 0.55:
            conflict_weight += 0.20
            reasons.append("directional_context_conflict: strong_relative_strength")
        if regime_name == Regime.TREND_UP.value and not reversal:
            conflict_weight += 0.25
            reasons.append("regime_conflict: short_against_trend_up")
        if slope_r is not None and slope_r >= SHORT_TERM_SLOPE_R and not reversal:
            conflict_weight += 0.18
            reasons.append("microstructure_conflict: short_term_upside_confirmation")

    evidence.update({"mover_bias": mover, "rs_score": rs, "short_term_slope_r": slope_r, "regime": regime_name, "direction_ok": direction_ok, "reversal_confirmed": reversal})
    return min(conflict_weight, 0.85), reasons, evidence


def _news_assessment(sig: Signal, df: pd.DataFrame) -> tuple[float, list[str], dict[str, Any]]:
    reasons: list[str] = []
    evidence: dict[str, Any] = {}
    available = bool(getattr(sig, "news_context_available", False))
    catalyst = getattr(sig, "news_catalyst", None)
    age = getattr(sig, "news_age_seconds", None)
    confidence = float(getattr(sig, "news_confidence", 0.0) or 0.0)
    direction = str(getattr(sig, "news_direction", "neutral") or "neutral")
    fresh = age is not None and age <= NEWS_FRESH_SECONDS
    material = catalyst in {"financing", "regulatory", "earnings"}
    reaction = _normalized_short_term_move(df)
    weight = 0.0
    adverse = (sig.side == "long" and direction == "bearish") or (sig.side == "short" and direction == "bullish")
    supportive = (sig.side == "long" and direction == "bullish") or (sig.side == "short" and direction == "bearish")

    if available and fresh and confidence >= NEWS_MATERIAL_CONFIDENCE:
        if adverse:
            absorbed = reaction is not None and ((sig.side == "long" and reaction >= SHORT_TERM_SLOPE_R) or (sig.side == "short" and reaction <= -SHORT_TERM_SLOPE_R))
            if not absorbed:
                weight = 0.45 if material else 0.20
                reasons.append(f"news_context_conflict: {'material' if material else 'fresh'}_{direction}_{catalyst or 'headline'}")
        elif supportive:
            weight = -0.10 if material else -0.05
            reasons.append(f"news_context_support: {direction}_{catalyst or 'headline'}")

    evidence.update({"news_available": available, "news_age_seconds": age, "news_confidence": confidence, "news_direction": direction, "news_catalyst": catalyst, "news_fresh": fresh, "news_material": material, "news_reaction_atr": reaction})
    return weight, reasons, evidence


def _entry_location_assessment(sig: Signal, df: pd.DataFrame) -> tuple[float, list[str], dict[str, Any]]:
    if len(df) < 21 or not {"close", "high", "low", "atr14"}.issubset(df.columns):
        return 0.0, [], {}
    close = float(df["close"].iloc[-1]); atr = float(df["atr14"].iloc[-1])
    if atr <= 0 or close <= 0:
        return 0.0, [], {}
    prior = df.iloc[-21:-1]; high20 = float(prior["high"].max()); low20 = float(prior["low"].min()); mover = getattr(sig, "mover_bias", None)
    reasons: list[str] = []; extension = 0.0
    if sig.side == "long" and close > high20 + EXTENSION_R * atr and mover != "gainer":
        extension = 0.15; reasons.append("entry_location_conflict: long_extended_without_mover_confirmation")
    elif sig.side == "short" and close < low20 - EXTENSION_R * atr and mover != "loser":
        extension = 0.15; reasons.append("entry_location_conflict: short_extended_without_mover_confirmation")
    return extension, reasons, {"high20": high20, "low20": low20, "close": close, "atr": atr}


def assess_entry_context(sig: Signal, df: pd.DataFrame, regime: Regime | str | None) -> EntryContextAssessment:
    """Return a weighted conviction decision at the final pre-risk boundary."""
    if sig.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
        return EntryContextAssessment(True)

    conviction = 1.0; reasons: list[str] = []; evidence: dict[str, Any] = {}
    directional_weight, directional_reasons, directional_evidence = _market_direction_conflict(sig, df, regime)
    conviction -= directional_weight; reasons.extend(directional_reasons); evidence.update(directional_evidence)
    news_weight, news_reasons, news_evidence = _news_assessment(sig, df)
    conviction -= news_weight; reasons.extend(news_reasons); evidence.update(news_evidence)
    extension_weight, extension_reasons, extension_evidence = _entry_location_assessment(sig, df)
    conviction -= extension_weight; reasons.extend(extension_reasons); evidence.update(extension_evidence)

    rs = getattr(sig, "rs_score", None); mover = getattr(sig, "mover_bias", None); regime_name = str(getattr(regime, "value", regime) or "").lower()
    if regime_name == Regime.RANGE.value and mover in {None, "neutral"} and (rs is None or 0.47 <= rs <= 0.53):
        conviction -= 0.08; reasons.append("macro_context_conflict: range_without_symbol_confirmation")
    if regime_name == Regime.VOLATILE.value and not evidence.get("direction_ok", False):
        conviction -= 0.10; reasons.append("macro_context_conflict: volatile_without_directional_confirmation")

    conviction = max(0.0, min(1.0, conviction))
    evidence["context_conflict_count"] = len(reasons); evidence["entry_conviction"] = round(conviction, 4); evidence["hard_conviction_floor"] = HARD_CONVICTION; evidence["minimum_conviction"] = MIN_CONVICTION
    allowed = conviction >= HARD_CONVICTION
    if not allowed:
        reasons.append("entry_conviction_hard_reject")
    elif conviction < MIN_CONVICTION:
        reasons.append("entry_conviction_degraded")
    return EntryContextAssessment(allowed, tuple(dict.fromkeys(reasons)), evidence)


def assess_thesis_continuity(db, sig: Signal, now: datetime) -> EntryContextAssessment:
    """Adapt same-session re-entry based on repeated same-thesis failures."""
    if db is None or sig.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
        return EntryContextAssessment(True)
    try:
        records = db.get_shadow_signals(limit=500)
    except Exception:
        return EntryContextAssessment(True)

    candidates: list[tuple[datetime, dict[str, Any], float]] = []
    for record in records:
        if str(record.get("symbol", "")).upper() != sig.symbol.upper() or record.get("direction") != sig.side:
            continue
        ts_raw = record.get("timestamp") or record.get("ts"); outcomes = record.get("outcomes") or {}
        if not ts_raw or not outcomes:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")) if isinstance(ts_raw, str) else ts_raw
            if ts.tzinfo is None: ts = ts.replace(tzinfo=UTC)
            if ts.date() != now.date(): continue
        except Exception:
            continue
        realized = None
        for horizon in ("60m", "15m", "close"):
            candidate = outcomes.get(horizon)
            if candidate and candidate.get("realized_r") is not None:
                realized = float(candidate["realized_r"]); break
        if realized is not None: candidates.append((ts, record, realized))

    if not candidates:
        return EntryContextAssessment(True)
    candidates.sort(key=lambda item: item[0], reverse=True)
    current_regime = str(getattr(getattr(sig, "regime", None), "value", getattr(sig, "regime", None)) or "").lower()
    current_mover = getattr(sig, "mover_bias", None); current_news_catalyst = getattr(sig, "news_catalyst", None); current_news_age = getattr(sig, "news_age_seconds", None); current_news_direction = getattr(sig, "news_direction", "neutral")
    fresh_material = current_news_catalyst in {"financing", "regulatory", "earnings"} and current_news_age is not None and current_news_age <= NEWS_FRESH_SECONDS
    recent = [row for row in candidates if (now - row[0]).total_seconds() <= REENTRY_LOOKBACK_MINUTES * 60]
    recent_losses = [row for row in recent if row[2] <= 0]
    same_context_losses = [row for row in recent_losses if str(row[1].get("regime") or row[1].get("market_regime") or "").lower() == current_regime and (current_mover is None or row[1].get("mover_bias") in {None, current_mover})]
    losses_today = [row for row in candidates if row[2] <= 0]

    reasons = ["thesis_history_observation: prior_same_day_outcome_available"]
    evidence = {"prior_same_day_losses": len(losses_today), "recent_same_direction_losses": len(recent_losses), "recent_same_context_losses": len(same_context_losses), "current_regime": current_regime, "current_mover_bias": current_mover, "fresh_material_catalyst": fresh_material, "current_news_direction": current_news_direction}
    if fresh_material and ((sig.side == "long" and current_news_direction == "bullish") or (sig.side == "short" and current_news_direction == "bearish")):
        evidence["reentry_policy"] = "fresh_supportive_catalyst_resets_thesis"; reasons.append("thesis_history_observation: fresh_supportive_catalyst_resets_reentry_penalty"); return EntryContextAssessment(True, tuple(reasons), evidence)
    if len(same_context_losses) >= REENTRY_HARD_BLOCK_COUNT:
        reasons.append("thesis_continuity_conflict: repeated_same_context_losses"); evidence["reentry_policy"] = "hard_block"; return EntryContextAssessment(False, tuple(reasons), evidence)
    if len(same_context_losses) >= REENTRY_LOSS_BLOCK_COUNT:
        reasons.append("thesis_continuity_conflict: repeated_same_context_losses"); evidence["reentry_policy"] = "adaptive_block"; return EntryContextAssessment(False, tuple(reasons), evidence)
    evidence["reentry_policy"] = "observation_only"
    return EntryContextAssessment(True, tuple(reasons), evidence)
