from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
import pandas as pd


class EntryQuality(StrEnum):
    EXCELLENT = "excellent"
    GOOD = "good"
    MARGINAL = "marginal"
    REJECT = "reject"


@dataclass(frozen=True)
class EntryQualityResult:
    quality: EntryQuality
    score: float

    trend_score: float
    location_score: float
    extension_score: float
    volume_score: float
    catalyst_score: float
    structure_score: float

    reasons: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.quality in {
            EntryQuality.EXCELLENT,
            EntryQuality.GOOD,
        }


def vwap_distance_pct(close: float, vwap: float) -> float:
    if vwap <= 0:
        return 0.0
    return ((close - vwap) / vwap) * 100.0


def impulse_extension_pct(close: float, reference_price: float) -> float:
    if reference_price <= 0:
        return 0.0
    return abs(close - reference_price) / reference_price * 100.0


def bullish_continuation(df: pd.DataFrame) -> bool:
    if len(df) < 5:
        return False
    a = df.iloc[-5:]
    # Check if we have ema columns
    has_ema = "ema9" in a.columns and "ema20" in a.columns
    ema_ok = (a["ema9"].iloc[-1] > a["ema20"].iloc[-1]) if has_ema else True
    
    return (
        a["close"].iloc[-1] > a["close"].iloc[-2]
        and a["low"].iloc[-2] >= a["low"].iloc[-4]
        and ema_ok
    )


def bearish_continuation(df: pd.DataFrame) -> bool:
    if len(df) < 5:
        return False
    a = df.iloc[-5:]
    has_ema = "ema9" in a.columns and "ema20" in a.columns
    ema_ok = (a["ema9"].iloc[-1] < a["ema20"].iloc[-1]) if has_ema else True

    return (
        a["close"].iloc[-1] < a["close"].iloc[-2]
        and a["high"].iloc[-2] <= a["high"].iloc[-4]
        and ema_ok
    )


def evaluate_entry_quality(
    *,
    side: str,
    close: float,
    vwap: float,
    atr: float,
    df: pd.DataFrame,
    vol_z: float,
    catalyst_score: float,
    trend_score: float,
    min_vwap_extension_pct: float = 1.75,
    hard_vwap_extension_pct: float = 3.00,
    max_atr_extension: float = 1.25,
    hard_atr_extension: float = 1.80,
) -> EntryQualityResult:

    reasons: list[str] = []

    if vwap <= 0 or atr <= 0:
        reasons.append("invalid vwap or atr")
        return EntryQualityResult(
            quality=EntryQuality.REJECT,
            score=0.0,
            trend_score=trend_score,
            location_score=0.0,
            extension_score=0.0,
            volume_score=0.0,
            catalyst_score=catalyst_score,
            structure_score=0.0,
            reasons=tuple(reasons)
        )

    vwap_distance = abs(close - vwap) / vwap * 100.0
    atr_extension = abs(close - vwap) / atr

    recent_high = float(df["high"].tail(20).max()) if len(df) >= 20 else close
    recent_low = float(df["low"].tail(20).min()) if len(df) >= 20 else close

    if side == "long":
        impulse_pct = max(0.0, (close - recent_low) / max(recent_low, 1e-9) * 100.0)
    else:
        impulse_pct = max(0.0, (recent_high - close) / max(recent_high, 1e-9) * 100.0)

    is_hard_reject = False
    if vwap_distance >= hard_vwap_extension_pct:
        reasons.append("vwap extension hard reject")
        is_hard_reject = True

    if atr_extension >= hard_atr_extension:
        reasons.append("atr extension hard reject")
        is_hard_reject = True

    if impulse_pct >= 10.0 and atr_extension >= 1.0:
        reasons.append("late impulse hard reject")
        is_hard_reject = True

    if is_hard_reject:
        return EntryQualityResult(
            quality=EntryQuality.REJECT,
            score=0.0,
            trend_score=trend_score,
            location_score=0.0,
            extension_score=0.0,
            volume_score=0.0,
            catalyst_score=catalyst_score,
            structure_score=0.0,
            reasons=tuple(reasons)
        )

    location_score = 100.0
    if vwap_distance <= 0.50:
        location_score = 100.0
    elif vwap_distance <= 1.00:
        location_score = 90.0
    elif vwap_distance <= min_vwap_extension_pct:
        location_score = 70.0
    else:
        location_score = 45.0

    if atr_extension <= 0.75:
        extension_score = 100.0
    elif atr_extension <= 1.00:
        extension_score = 90.0
    elif atr_extension <= max_atr_extension:
        extension_score = 70.0
    elif atr_extension <= hard_atr_extension:
        extension_score = 45.0
    else:
        extension_score = 0.0

    if vol_z >= 2.0:
        volume_score = 100.0
    elif vol_z >= 1.0:
        volume_score = 80.0
    elif vol_z >= 0.0:
        volume_score = 60.0
    else:
        volume_score = 30.0

    continuation = bullish_continuation(df) if side == "long" else bearish_continuation(df)
    structure_score = 100.0 if continuation else 40.0

    score = (
        0.25 * trend_score
        + 0.25 * location_score
        + 0.20 * extension_score
        + 0.10 * volume_score
        + 0.10 * catalyst_score
        + 0.10 * structure_score
    )

    if score >= 82:
        quality = EntryQuality.EXCELLENT
    elif score >= 70:
        quality = EntryQuality.GOOD
    elif score >= 58:
        quality = EntryQuality.MARGINAL
    else:
        quality = EntryQuality.REJECT
        reasons.append(f"score {score:.2f} too low")

    return EntryQualityResult(
        quality=quality,
        score=round(score, 2),
        trend_score=trend_score,
        location_score=location_score,
        extension_score=extension_score,
        volume_score=volume_score,
        catalyst_score=catalyst_score,
        structure_score=structure_score,
        reasons=tuple(reasons),
    )
