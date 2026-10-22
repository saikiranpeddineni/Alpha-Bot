"""PIS Profit Memory and Excursion Tracker.

Tracks Peak R, Trough R, Maximum Favorable Excursion (MFE), and Maximum Adverse Excursion (MAE).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.pis.models import PositionObservation


@dataclass(frozen=True)
class ProfitMemoryState:
    """Immutable state snapshot for profit memory."""

    peak_r: float
    trough_r: float
    mfe_price: float
    mae_price: float


class ProfitMemoryTracker:
    """Tracks excursion metrics deterministically."""

    @staticmethod
    def update(
        current_state: ProfitMemoryState,
        obs: PositionObservation,
    ) -> ProfitMemoryState:
        """Update profit memory state with a new observation.

        Args:
            current_state: Existing ProfitMemoryState.
            obs: PositionObservation.

        Returns:
            Updated ProfitMemoryState.
        """
        entry = obs.entry_price
        risk = obs.risk_per_share
        side = obs.side

        if risk <= 1e-8 or math.isnan(risk) or math.isinf(risk):
            return current_state

        # High/Low excursion calculation
        if side > 0:  # Long
            high_r = (obs.high - entry) / risk
            low_r = (obs.low - entry) / risk
            new_mfe_price = max(current_state.mfe_price, obs.high)
            new_mae_price = min(current_state.mae_price, obs.low)
        else:  # Short
            high_r = (entry - obs.low) / risk
            low_r = (entry - obs.high) / risk
            new_mfe_price = min(current_state.mfe_price, obs.low)
            new_mae_price = max(current_state.mae_price, obs.high)

        # Peak/trough memory is deliberately derived from the completed bar's
        # high/low only.  ``current_price`` can be a fresher broker quote than
        # the bar row (and replay inputs can contain a later snapshot); allowing
        # ``obs.r_multiple`` to update excursion memory would turn that quote
        # into a lookahead-like high-water mark.  Current R remains available to
        # health, thesis, and hard-stop decisions, while excursion memory stays
        # causal to observed completed bars.
        new_peak_r = max(current_state.peak_r, high_r)
        new_trough_r = min(current_state.trough_r, low_r)

        # Sanitize against NaN / inf
        if math.isnan(new_peak_r) or math.isinf(new_peak_r):
            new_peak_r = current_state.peak_r
        if math.isnan(new_trough_r) or math.isinf(new_trough_r):
            new_trough_r = current_state.trough_r

        return ProfitMemoryState(
            peak_r=new_peak_r,
            trough_r=new_trough_r,
            mfe_price=new_mfe_price,
            mae_price=new_mae_price,
        )

    @staticmethod
    def initial_state(obs: PositionObservation) -> ProfitMemoryState:
        """Initialize profit memory from initial observation."""
        # The initial snapshot is also the entry-window baseline, so preserve
        # its current-state R when the quote is represented by this bar.  If a
        # broker quote is clearly outside the completed bar, use the bar's
        # extremes instead; that quote is fresher than the observed bar and
        # must not seed the first max-R high-water mark.
        quote_in_bar = obs.low <= obs.current_price <= obs.high
        if quote_in_bar:
            peak_r = obs.r_multiple
            trough_r = obs.r_multiple
            mfe_price = obs.current_price
            mae_price = obs.current_price
        else:
            risk = obs.risk_per_share
            if risk <= 1e-8 or not math.isfinite(risk):
                return ProfitMemoryState(
                    peak_r=0.0,
                    trough_r=0.0,
                    mfe_price=obs.current_price,
                    mae_price=obs.current_price,
                )

            if obs.side > 0:
                peak_r = (obs.high - obs.entry_price) / risk
                trough_r = (obs.low - obs.entry_price) / risk
                mfe_price = obs.high
                mae_price = obs.low
            else:
                peak_r = (obs.entry_price - obs.low) / risk
                trough_r = (obs.entry_price - obs.high) / risk
                mfe_price = obs.low
                mae_price = obs.high

        return ProfitMemoryState(
            peak_r=max(0.0, peak_r) if math.isfinite(peak_r) else 0.0,
            trough_r=min(0.0, trough_r) if math.isfinite(trough_r) else 0.0,
            mfe_price=mfe_price,
            mae_price=mae_price,
        )
