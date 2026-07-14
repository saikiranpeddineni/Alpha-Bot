"""Signal Shadow Ledger & Forward Outcome Tracker.
"""

from __future__ import annotations

import uuid
from datetime import datetime
import pandas as pd
import numpy as np

from app.core.phase import classify_clock_phase, classify_market_behavior
from app.core.watchlist_mapper import OpportunityMapper, OpportunityClass


class ShadowLedger:
    @staticmethod
    def calculate_forward_outcomes(
        df: pd.DataFrame,
        i: int,
        direction: str,
        stop_loss: float | None,
        entry_price: float,
    ) -> dict:
        """Calculate causal future returns, MAE, MFE, and R multiples at offsets

        1m, 5m, 15m, 30m, 60m, and session close.
        """
        outcomes = {}
        if i + 1 >= len(df):
            return outcomes

        # Determine initial risk per share (immutable for R calculations)
        if stop_loss is None or stop_loss == entry_price:
            initial_stop = entry_price * (0.985 if direction == "long" else 1.015)
        else:
            initial_stop = stop_loss

        # Enforce stop-loss sign sanity relative to entry price (gap protection)
        if direction == "long" and initial_stop >= entry_price:
            initial_stop = entry_price * 0.985
        elif direction == "short" and initial_stop <= entry_price:
            initial_stop = entry_price * 1.015

        initial_risk = abs(entry_price - initial_stop)
        # Prevent denominator inflation: minimum risk distance of 0.2% of entry or $0.05
        initial_risk = max(initial_risk, entry_price * 0.002, 0.05)

        # Offsets in bars (1 bar = 1 min)
        offsets = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "60m": 60,
        }

        # Also add session close outcome
        current_date = df.index[i].date()
        day_bars = df[df.index.date == current_date]
        if not day_bars.empty:
            close_idx = df.index.get_loc(day_bars.index[-1])
            session_close_offset = max(1, close_idx - (i + 1))
            offsets["close"] = session_close_offset

        for name, H in offsets.items():
            end_idx = min(len(df), i + 1 + H)
            slice_df = df.iloc[i + 1 : end_idx]
            if slice_df.empty:
                continue

            mfe_price = entry_price
            mae_price = entry_price
            stop_hit = False
            exit_price = None

            for _, bar in slice_df.iterrows():
                high_val = float(bar["high"])
                low_val = float(bar["low"])

                if direction == "long":
                    if low_val <= initial_stop:
                        stop_hit = True
                        exit_price = initial_stop
                        mae_price = min(mae_price, initial_stop)
                        mfe_price = max(mfe_price, high_val)
                        break
                    else:
                        mfe_price = max(mfe_price, high_val)
                        mae_price = min(mae_price, low_val)
                else:  # short
                    if high_val >= initial_stop:
                        stop_hit = True
                        exit_price = initial_stop
                        mae_price = max(mae_price, initial_stop)
                        mfe_price = min(mfe_price, low_val)
                        break
                    else:
                        mfe_price = min(mfe_price, low_val)
                        mae_price = max(mae_price, high_val)

            if stop_hit:
                pnl = -initial_risk
            else:
                final_close = float(slice_df["close"].iloc[-1])
                pnl = (final_close - entry_price) if direction == "long" else (entry_price - final_close)

            if direction == "long":
                mfe = mfe_price - entry_price
                mae = entry_price - mae_price
            else:
                mfe = entry_price - mfe_price
                mae = mae_price - entry_price

            mfe_r = mfe / initial_risk
            mae_r = mae / initial_risk
            realized_r = pnl / initial_risk
            fwd_ret = pnl / entry_price if entry_price > 0 else 0.0

            winner_capture_ratio = realized_r / mfe_r if mfe_r > 0 else 0.0
            gave_back_r = mfe_r - realized_r
            gave_back_pct_of_mfe = (gave_back_r / mfe_r * 100.0) if mfe_r > 0 else 0.0

            outcomes[name] = {
                "fwd_return": round(fwd_ret, 6),
                "mfe": round(mfe, 4),
                "mae": round(mae, 4),
                "mfe_r": round(mfe_r, 4),
                "mae_r": round(mae_r, 4),
                "realized_r": round(realized_r, 4),
                "winner_capture_ratio": round(winner_capture_ratio, 4),
                "gave_back_r": round(gave_back_r, 4),
                "gave_back_pct_of_mfe": round(gave_back_pct_of_mfe, 2),
                "mfe_before_loss_r": round(mfe_r, 4) if realized_r <= 0 else 0.0,
            }

        return outcomes

    @staticmethod
    def record_candidate(
        db_engine,
        symbol: str,
        ts: datetime,
        strategy: str,
        direction: str,
        signal_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        ml_confidence: float | None,
        final_decision: str,
        rejection_stage: str,
        rejection_reason: str,
        df: pd.DataFrame,
        spy_df: pd.DataFrame,
        slot_rvol_val: float | None,
        vwap_telemetry: dict,
        mode: str = "backtest",
        candidate_id: str | None = None,
    ) -> dict:
        """Assembles a telemetry record, computes future outcomes, and saves it to the DB."""
        if candidate_id is None:
            # unique prefix: symbol_strat_date_hour_min_sec
            prefix = f"{symbol}_{strategy}_{ts.strftime('%Y%m%d_%H%M%S')}"
            candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()

        clock_phase = classify_clock_phase(ts)
        behavior_state = classify_market_behavior(spy_df, ts)

        # Classify opportunity mapper class using premarket subset
        # We can extract premarket bars for today (up to 09:30 AM ET)
        prior_close = None
        open_price = None
        premarket_df = pd.DataFrame()
        
        # Causal logic: get current day premarket bars
        if df is not None and not df.empty:
            ts_date = ts.date()
            day_bars = df[df.index.date == ts_date]
            if not day_bars.empty:
                # Prior close
                prior_bars = df[df.index.date < ts_date]
                if not prior_bars.empty:
                    prior_close = float(prior_bars["close"].iloc[-1])
                # Premarket bars
                premarket_df = day_bars[day_bars.index < day_bars.index[0].replace(hour=9, minute=30)]
                # Regular session open price
                reg_bars = day_bars[day_bars.index >= day_bars.index[0].replace(hour=9, minute=30)]
                if not reg_bars.empty:
                    open_price = float(reg_bars["open"].iloc[0])

        opp_class = OpportunityMapper.classify_symbol(premarket_df, prior_close, open_price)

        # Counterfactual entry price: the open of next bar (i+1)
        entry_price = signal_price
        i = -1
        if df is not None and not df.empty:
            try:
                # Find index of ts
                i = df.index.get_loc(ts)
                if i + 1 < len(df):
                    entry_price = float(df["open"].iloc[i + 1])
            except Exception:
                pass

        # Calculate outcomes
        outcomes = {}
        if df is not None and not df.empty and i != -1:
            outcomes = ShadowLedger.calculate_forward_outcomes(df, i, direction, stop_loss, entry_price)

        payload = {
            "candidate_id": candidate_id,
            "timestamp": ts.isoformat(),
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,
            "signal_price": round(signal_price, 4),
            "entry_price": round(entry_price, 4),
            "clock_phase": clock_phase,
            "behavior_state": behavior_state,
            "opportunity_class": opp_class,
            "ml_confidence": round(ml_confidence, 4) if ml_confidence is not None else None,
            "final_decision": final_decision,
            "rejection_stage": rejection_stage,
            "rejection_reason": rejection_reason,
            "vwap_state": vwap_telemetry.get("vwap_state"),
            "vwap_distance_atr": vwap_telemetry.get("vwap_distance_atr"),
            "vwap_slope": vwap_telemetry.get("vwap_slope"),
            "vwap_cross_count": vwap_telemetry.get("vwap_cross_count"),
            "slot_rvol": round(slot_rvol_val, 4) if slot_rvol_val is not None else None,
            "outcomes": outcomes,
            "vwap_telemetry": vwap_telemetry,
        }

        # Save to SQLite database if available
        if db_engine is not None:
            try:
                db_engine.save_shadow_signal(
                    candidate_id=candidate_id,
                    ts=ts,
                    symbol=symbol,
                    strategy=strategy,
                    direction=direction,
                    mode=mode,
                    payload=payload,
                )
            except Exception:
                pass

        return payload
