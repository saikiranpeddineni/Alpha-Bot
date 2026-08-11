"""Contextual entry intelligence for Champion candidates.

This module uses evidence already present on the existing signal/context path.
It does not change ML, Meta, PIS, risk, or execution thresholds.

The guiding principle is evidence over emotion: prior outcomes are context,
not a permanent veto. Current market evidence remains authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction


@dataclass(frozen=True)
class EntryContextAssessment:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    evidence: dict[str, float | str | bool | None] = field(default_factory=dict)


def _normalized_short_term_move(df: pd.DataFrame) -> float | None:
    """Return the latest 4-bar move normalized by the current ATR."""
    if len(df) < 5 or "close" not in df.columns or "atr14" not in df.columns:
        return None
    atr = float(df["atr14"].iloc[-1])
    if atr <= 0:
        return None
    closes = df["close"].astype(float)
    return (float(closes.iloc[-1]) - float(closes.iloc[-5])) / max(atr, 1e-9)


def _reversal_confirmed(sig: Signal, df: pd.DataFrame) -> bool:
    """Return True when a counter-trend signal has actual structure confirmation."""
    if len(df) < 8 or "atr14" not in df.columns or "close" not in df.columns:
        return False
    atr = float(df["atr14"].iloc[-1])
    if atr <= 0:
        return False

    closes = df["close"].astype(float)
    last = float(closes.iloc[-1])
    prior = closes.iloc[-5:-1]
    slope_r = (last - float(closes.iloc[-5])) / max(atr, 1e-9)
    prev_high = float(df["high"].iloc[-7:-1].max()) if "high" in df.columns else float(prior.max())
    prev_low = float(df["low"].iloc[-7:-1].min()) if "low" in df.columns else float(prior.min())

    if sig.side == "long":
        return slope_r >= 0.10 and last > prev_high
    return slope_r <= -0.10 and last < prev_low


def _directional_contradictions(sig: Signal, df: pd.DataFrame) -> list[str]:
    reasons: list[str] = []
    side = sig.side
    mover = getattr(sig, "mover_bias", None)
    rs = getattr(sig, "rs_score", None)

    if side == "long" and mover == "loser" and rs is not None and rs < 0.45:
        reasons.append("directional_context_conflict: loser_mover_and_weak_relative_strength")
    elif side == "short" and mover == "gainer" and rs is not None and rs > 0.55:
        reasons.append("directional_context_conflict: gainer_mover_and_strong_relative_strength")

    if "ema9" in df.columns and "ema20" in df.columns:
        ema9 = float(df["ema9"].iloc[-1])
        ema20 = float(df["ema20"].iloc[-1])
        reversal = _reversal_confirmed(sig, df)
        slope_conflict = False
        slope_r = _normalized_short_term_move(df)
        if slope_r is not None:
            slope_conflict = slope_r <= -0.10 if side == "long" else slope_r >= 0.10
        if side == "long" and ema9 < ema20 and slope_conflict and not reversal:
            reasons.append("trend_context_conflict: confirmed_short_term_trend_opposes_long")
        elif side == "short" and ema9 > ema20 and slope_conflict and not reversal:
            reasons.append("trend_context_conflict: confirmed_short_term_trend_opposes_short")

    news_available = bool(getattr(sig, "news_context_available", False))
    news_catalyst = getattr(sig, "news_catalyst", None)
    if news_available and news_catalyst:
        age = getattr(sig, "news_age_seconds", None)
        confidence = float(getattr(sig, "news_confidence", 0.0) or 0.0)
        direction = str(getattr(sig, "news_direction", "neutral") or "neutral")
        fresh = age is not None and age <= 1800
        material = news_catalyst in {"financing", "regulatory", "earnings"}
        if fresh and material and confidence >= 0.55:
            reaction = _normalized_short_term_move(df)
            adverse = (side == "long" and direction == "bearish") or (side == "short" and direction == "bullish")
            if adverse:
                absorbed = reaction is not None and (
                    (side == "long" and reaction >= 0.10)
                    or (side == "short" and reaction <= -0.10)
                )
                if not absorbed:
                    reasons.append(f"news_context_conflict: fresh_material_{direction}_{news_catalyst}_catalyst")

    return reasons


def _macro_context_conflicts(sig: Signal, df: pd.DataFrame, regime: Regime | str | None) -> list[str]:
    """Add one soft macro/chop contradiction only when the setup is already weak."""
    regime_name = getattr(regime, "value", str(regime) if regime is not None else "")
    if getattr(sig, "strategy", None) not in {"momentum", "gap_and_go", "trend_pullback"}:
        return []
    if str(regime_name).upper() not in {"RANGE", "VOLATILE"}:
        return []
    rs = getattr(sig, "rs_score", None)
    mover = getattr(sig, "mover_bias", None)
    if rs is None and not mover:
        return ["macro_context_conflict: choppy_regime_without_symbol_confirmation"]
    if rs is not None and 0.47 <= rs <= 0.53 and mover in {None, "neutral"}:
        return ["macro_context_conflict: choppy_regime_and_neutral_symbol_confirmation"]
    return []


def _extension_conflicts(sig: Signal, df: pd.DataFrame) -> list[str]:
    """Flag only genuine range-edge extension, not ordinary tests of resistance/support."""
    if len(df) < 20 or "atr14" not in df.columns or "close" not in df.columns:
        return []
    if "high" not in df.columns or "low" not in df.columns:
        return []
    close = float(df["close"].iloc[-1])
    atr = float(df["atr14"].iloc[-1])
    if atr <= 0 or close <= 0:
        return []

    lookback = df.iloc[-20:]
    high20 = float(lookback["high"].max())
    low20 = float(lookback["low"].min())
    side = sig.side
    mover = getattr(sig, "mover_bias", None)
    # Require a true extension beyond the recent range by 0.10 ATR. Merely touching
    # the prior high/low is not evidence of exhaustion and should not suppress a
    # legitimate breakout opportunity.
    if side == "long" and close > high20 + 0.10 * atr and mover != "gainer":
        return ["entry_location_conflict: long_is_extended_without_mover_confirmation"]
    if side == "short" and close < low20 - 0.10 * atr and mover != "loser":
        return ["entry_location_conflict: short_is_extended_without_mover_confirmation"]
    return []


def assess_entry_context(sig: Signal, df: pd.DataFrame, regime: Regime | str | None) -> EntryContextAssessment:
    """Reject only when independent context signals materially contradict a thesis."""
    if sig.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
        return EntryContextAssessment(True)

    reasons = _directional_contradictions(sig, df)
    all_reasons = reasons + _macro_context_conflicts(sig, df, regime) + _extension_conflicts(sig, df)
    evidence = {
        "regime": getattr(regime, "value", str(regime) if regime is not None else None),
        "mover_bias": getattr(sig, "mover_bias", None),
        "rs_score": getattr(sig, "rs_score", None),
        "news_direction": getattr(sig, "news_direction", "neutral"),
        "news_sentiment": getattr(sig, "news_sentiment", None),
        "news_confidence": getattr(sig, "news_confidence", 0.0),
        "news_catalyst": getattr(sig, "news_catalyst", None),
        "news_age_seconds": getattr(sig, "news_age_seconds", None),
        "news_reaction_atr": _normalized_short_term_move(df),
        "context_conflict_count": len(all_reasons),
    }
    if len(all_reasons) >= 2:
        return EntryContextAssessment(False, tuple(all_reasons), evidence)
    return EntryContextAssessment(True, tuple(all_reasons), evidence)


def assess_thesis_continuity(db, sig: Signal, now) -> EntryContextAssessment:
    """Use prior outcomes as adaptive evidence, never as an emotional hard veto.

    The engine already has contextual expectancy/ML adaptation for historical
    performance. This function therefore records the latest same-day outcome
    as evidence but does not automatically block a new candidate merely because
    an earlier attempt lost. A current thesis must be judged by current market
    evidence; otherwise a losing trade would create path-dependent behavior.
    """
    if db is None or sig.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
        return EntryContextAssessment(True)

    try:
        records = db.get_shadow_signals(limit=500)
    except Exception:
        return EntryContextAssessment(True)

    direction = sig.side
    current_regime = getattr(sig.regime, "value", str(sig.regime) if sig.regime is not None else None)
    candidates = []
    for record in records:
        if str(record.get("symbol", "")).upper() != sig.symbol.upper():
            continue
        if record.get("direction") != direction:
            continue
        outcomes = record.get("outcomes") or {}
        if not outcomes:
            continue
        ts = record.get("timestamp") or record.get("ts")
        if ts is None:
            continue
        try:
            if isinstance(ts, str):
                from datetime import datetime
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.date() != now.date():
                continue
        except Exception:
            continue

        realized = None
        for horizon in ("60m", "15m", "close"):
            if horizon in outcomes and outcomes[horizon].get("realized_r") is not None:
                realized = float(outcomes[horizon]["realized_r"])
                break
        if realized is not None:
            candidates.append((ts, record, realized))

    if not candidates:
        return EntryContextAssessment(True)

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, latest, realized = candidates[0]
    previous_regime = latest.get("regime") or latest.get("market_regime")
    previous_mover = latest.get("mover_bias")

    # Keep this history visible to telemetry/shadow analysis, but do not turn a
    # prior loss into a hard block. A new signal is a new decision point.
    return EntryContextAssessment(
        True,
        ("thesis_history_observation: prior_same_day_outcome_available",),
        {
            "previous_realized_r": realized,
            "previous_regime": previous_regime,
            "current_regime": current_regime,
            "previous_mover_bias": previous_mover,
            "current_mover_bias": getattr(sig, "mover_bias", None),
        },
    )
