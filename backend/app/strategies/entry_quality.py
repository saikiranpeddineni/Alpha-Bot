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
) -> EntryQualityResult:

    reasons: list[str] = []

    location = 100.0
    extension = 100.0
    volume = 50.0
    structure = 50.0

    distance_pct = abs(vwap_distance_pct(close, vwap))
    atr_extension = abs(close - vwap) / max(atr, 1e-9)
    
    # Location
    if distance_pct <= 0.50:
        location += 5
    elif distance_pct <= 1.25:
        location += 0
    elif distance_pct <= 1.75:
        location -= 15
        reasons.append("moderately extended from VWAP")
    else:
        location -= 35
        reasons.append("extended from VWAP")

    # ATR extension
    if atr_extension <= 0.75:
        extension = 100
    elif atr_extension <= 1.25:
        extension = 80
    elif atr_extension <= 1.80:
        extension = 55
        reasons.append("ATR extension elevated")
    else:
        extension = 20
        reasons.append("extreme ATR extension")

    # Volume
    if vol_z >= 2.0:
        volume = 100
    elif vol_z >= 1.0:
        volume = 75
    elif vol_z >= 0:
        volume = 55
    else:
        volume = 25

    # Structure
    structure = (
        100 if
        (
            bullish_continuation(df)
            if side == "long"
            else bearish_continuation(df)
        )
        else 40
    )
    
    # Late stage impulse check
    if len(df) >= 20:
        recent_high = float(df["high"].iloc[-20:].max())
        recent_low = float(df["low"].iloc[-20:].min())
        
        if side == "long":
            impulse_pct = (close - recent_low) / max(recent_low, 1e-9) * 100.0
        else:
            impulse_pct = (recent_high - close) / max(recent_high, 1e-9) * 100.0
            
        if impulse_pct > 8.0 and atr_extension > 1.0:
            reasons.append("late-stage impulse; continuation risk poor")
            # Penalize heavily if it's a late chase
            location -= 40
            extension -= 40

    score = (
        0.25 * trend_score
        + 0.25 * location
        + 0.20 * extension
        + 0.10 * volume
        + 0.10 * catalyst_score
        + 0.10 * structure
    )

    if score >= 82:
        quality = EntryQuality.EXCELLENT
    elif score >= 70:
        quality = EntryQuality.GOOD
    elif score >= 58:
        quality = EntryQuality.MARGINAL
    else:
        quality = EntryQuality.REJECT

    return EntryQualityResult(
        quality=quality,
        score=round(score, 2),
        trend_score=trend_score,
        location_score=location,
        extension_score=extension,
        volume_score=volume,
        catalyst_score=catalyst_score,
        structure_score=structure,
        reasons=tuple(reasons),
    )
