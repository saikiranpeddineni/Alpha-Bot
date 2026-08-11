"""Contextual entry intelligence for Champion candidates.

The module does not tune ML/Meta thresholds. It evaluates whether a candidate
is internally consistent with the market/symbol context and records evidence
for the existing decision pipeline.
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


def _directional_contradictions(sig: Signal, df: pd.DataFrame) -> list[str]:
    reasons: list[str] = []
    side = sig.side
    mover = sig.mover_bias
    rs = sig.rs_score

    if side == "long" and mover == "loser" and rs is not None and rs < 0.45:
        reasons.append("directional_context_conflict: loser_mover_and_weak_relative_strength")
    elif side == "short" and mover == "gainer" and rs is not None and rs > 0.55:
        reasons.append("directional_context_conflict: gainer_mover_and_strong_relative_strength")

    if "ema9" in df.columns and "ema20" in df.columns:
        ema9 = float(df["ema9"].iloc[-1])
        ema20 = float(df["ema20"].iloc[-1])
        if side == "long" and ema9 < ema20:
            reasons.append("trend_context_conflict: short_term_trend_opposes_long")
        elif side == "short" and ema9 > ema20:
            reasons.append("trend_context_conflict: short_term_trend_opposes_short")

    return reasons


def _extension_conflicts(sig: Signal, df: pd.DataFrame) -> list[str]:
    if len(df) < 20 or "atr14" not in df.columns:
        return []
    close = float(df["close"].iloc[-1])
    atr = float(df["atr14"].iloc[-1])
    if atr <= 0 or close <= 0:
        return []

    lookback = df.iloc[-20:]
    high20 = float(lookback["high"].max())
    low20 = float(lookback["low"].min())
    side = sig.side

    # A breakout at an extreme is acceptable when scanner mover context confirms
    # the same direction. Otherwise it is treated as an entry-location conflict.
    if side == "long" and close >= high20 - 0.25 * atr and sig.mover_bias != "gainer":
        return ["entry_location_conflict: long_is_extended_without_mover_confirmation"]
    if side == "short" and close <= low20 + 0.25 * atr and sig.mover_bias != "loser":
        return ["entry_location_conflict: short_is_extended_without_mover_confirmation"]
    return []


def assess_entry_context(sig: Signal, df: pd.DataFrame, regime: Regime | str | None) -> EntryContextAssessment:
    """Reject only when independent context signals materially contradict a thesis.

    A single weak contextual signal never vetoes a Champion candidate. The
    boundary requires at least two independent contradictions.
    """
    if sig.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
        return EntryContextAssessment(True)

    reasons = _directional_contradictions(sig, df)
    all_reasons = reasons + _extension_conflicts(sig, df)
    if len(all_reasons) >= 2:
        return EntryContextAssessment(
            False,
            tuple(all_reasons),
            {
                "regime": getattr(regime, "value", str(regime) if regime is not None else None),
                "mover_bias": sig.mover_bias,
                "rs_score": sig.rs_score,
                "context_conflict_count": len(all_reasons),
            },
        )

    return EntryContextAssessment(
        True,
        tuple(all_reasons),
        {
            "regime": getattr(regime, "value", str(regime) if regime is not None else None),
            "mover_bias": sig.mover_bias,
            "rs_score": sig.rs_score,
            "context_conflict_count": len(all_reasons),
        },
    )


def assess_thesis_continuity(db, sig: Signal, now) -> EntryContextAssessment:
    """Block repeated same-session negative theses, not trades by elapsed time.

    A new trade is allowed when direction, regime, or mover context materially
    changes. This is intentionally evidence-based rather than a fixed cooldown.
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
    if realized >= 0:
        return EntryContextAssessment(True)

    previous_regime = latest.get("regime") or latest.get("market_regime")
    if previous_regime and current_regime and str(previous_regime) != str(current_regime):
        return EntryContextAssessment(True)

    previous_mover = latest.get("mover_bias")
    if previous_mover and sig.mover_bias and previous_mover != sig.mover_bias:
        return EntryContextAssessment(True)

    return EntryContextAssessment(
        False,
        ("thesis_continuity_conflict: latest_same_day_same_direction_thesis_was_negative",),
        {
            "previous_realized_r": realized,
            "previous_regime": previous_regime,
            "current_regime": current_regime,
            "previous_mover_bias": previous_mover,
            "current_mover_bias": sig.mover_bias,
        },
    )
