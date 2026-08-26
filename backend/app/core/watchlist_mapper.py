"""Watchlist opportunity classification engine.
"""

from __future__ import annotations

from enum import StrEnum
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")


class OpportunityClass(StrEnum):
    MOMENTUM_TREND = "momentum_trend"
    MEAN_REVERSION_RANGE = "mean_reversion_range"
    EVENT_HIGH_VOLATILITY = "event_high_volatility"
    UNCERTAIN = "uncertain"


class OpportunityMapper:
    @staticmethod
    def classify_symbol(
        premarket_df: pd.DataFrame,
        prior_close: float | None,
        open_price: float | None,
    ) -> OpportunityClass:
        """Classify a symbol based on its premarket and gap structure.

        P0/P2 compliance: checks path efficiency and gaps.
        """
        if premarket_df.empty or len(premarket_df) < 5:
            return OpportunityClass.UNCERTAIN

        # Filter bars within premarket (04:00 - 09:30 ET)
        et_idx = premarket_df.index.tz_convert(ET)
        pm_mask = (et_idx.hour * 60 + et_idx.minute) < (9 * 60 + 30)
        pm_bars = premarket_df[pm_mask]

        if len(pm_bars) < 5:
            return OpportunityClass.UNCERTAIN

        close_prices = pm_bars["close"].ffill().bfill().to_numpy()
        if len(close_prices) < 2:
            return OpportunityClass.UNCERTAIN

        p_first = close_prices[0]
        p_last = close_prices[-1]

        # Calculate premarket path efficiency
        abs_net = abs(p_last - p_first)
        abs_sum_diff = float(np.sum(np.abs(np.diff(close_prices))))

        efficiency = abs_net / abs_sum_diff if abs_sum_diff > 0 else 0.0

        # Calculate gap %
        gap_pct = 0.0
        if prior_close and open_price and prior_close > 0:
            gap_pct = (open_price / prior_close - 1.0) * 100.0

        # Heuristic rules:
        # High gap, directional efficiency trend
        if abs(gap_pct) >= 1.5:
            if efficiency >= 0.5:
                return OpportunityClass.MOMENTUM_TREND
            elif efficiency < 0.3:
                return OpportunityClass.EVENT_HIGH_VOLATILITY

        if abs(gap_pct) < 0.5 and efficiency < 0.3:
            return OpportunityClass.MEAN_REVERSION_RANGE

        return OpportunityClass.UNCERTAIN
