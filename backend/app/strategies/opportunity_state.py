from enum import StrEnum


class OpportunityState(StrEnum):
    NORMAL = "normal"
    TREND = "trend"
    EVENT = "event"
    PRICE_DISCOVERY = "price_discovery"
    EXTENDED = "extended"
    REVERSAL = "reversal"
    NO_TRADE = "no_trade"


def classify_opportunity(
    *,
    atr_pct: float,
    gap_pct: float,
    news_catalyst: bool,
    volume_z: float,
    extension_atr: float,
    trend_score: float,
    news_confidence: float = 0.0,
    event_age_minutes: float | None = None,
) -> OpportunityState:
    
    if news_catalyst and volume_z >= 2.0 and atr_pct >= 6.0:
        return OpportunityState.PRICE_DISCOVERY

    if abs(gap_pct) >= 8.0 and extension_atr >= 1.25:
        return OpportunityState.PRICE_DISCOVERY

    if extension_atr >= 1.80:
        return OpportunityState.EXTENDED

    if news_catalyst and (event_age_minutes is None or event_age_minutes <= 180):
        return OpportunityState.EVENT

    if trend_score >= 75:
        return OpportunityState.TREND

    return OpportunityState.NORMAL
