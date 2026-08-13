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
) -> OpportunityState:
    
    # Event + massive volatility and expansion -> PRICE_DISCOVERY
    if news_catalyst and atr_pct >= 4.0 and volume_z >= 2.0:
        return OpportunityState.PRICE_DISCOVERY

    if news_catalyst:
        return OpportunityState.EVENT

    if extension_atr >= 1.8:
        return OpportunityState.EXTENDED

    if trend_score >= 75:
        return OpportunityState.TREND

    return OpportunityState.NORMAL
