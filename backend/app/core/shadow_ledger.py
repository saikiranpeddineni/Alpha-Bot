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
        strategy_params: dict | None = None,
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
            "strategy_params": strategy_params,
            "replayed": False,
            "replay_timestamp": None,
            "simulated_pnl": None,
            "mfe_pct": None,
            "mae_pct": None,
            "exit_reason": None,
            "tp_hit": None,
            "sl_hit": None,
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

    @staticmethod
    def replay_signal_60m(
        df: pd.DataFrame,
        entry_idx: int,
        direction: str,
        stop_loss: float | None,
        take_profit: float | None,
        entry_price: float,
        strategy_name: str | None = None,
        strategy_params: dict | None = None,
    ) -> dict:
        """Simulate a 60-minute trade path to calculate MFE, MAE, TP/SL triggers, trailing stops, and P&L.
        Runs strategy-specific exits by instantiating the strategy and calling on_bar.
        """
        if entry_idx >= len(df):
            return {}

        # Set default stop loss if none is provided
        if stop_loss is None or stop_loss == entry_price:
            initial_stop = entry_price * (0.985 if direction == "long" else 1.015)
        else:
            initial_stop = stop_loss

        # Sanity check stop loss
        if direction == "long" and initial_stop >= entry_price:
            initial_stop = entry_price * 0.985
        elif direction == "short" and initial_stop <= entry_price:
            initial_stop = entry_price * 1.015

        # Path of the next 60 minutes
        slice_df = df.iloc[entry_idx : entry_idx + 60]
        if slice_df.empty:
            return {}

        # Instantiate strategy if strategy exit logic is needed
        strategy_instance = None
        if strategy_name and strategy_params is not None:
            try:
                from app.strategies.base import REGISTRY
                strat_cls = REGISTRY.get(strategy_name)
                if strat_cls:
                    strategy_instance = strat_cls(params=strategy_params)
            except Exception:
                pass

        # Set up simulated position for strategy.on_bar exits
        from app.core.domain import Position, SignalAction
        from app.strategies.base import StrategyContext
        sim_pos = Position(
            symbol=str(slice_df.iloc[0].get("symbol", "UNKNOWN")),
            avg_entry_price=entry_price,
            qty=100 if direction == "long" else -100,
            market_price=entry_price,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            stop_loss=initial_stop,
            take_profit=take_profit,
            strategy=strategy_name or "unknown",
        )

        mfe_price = entry_price
        mae_price = entry_price
        mfe_time = None
        mae_time = None
        tp_triggered = False
        sl_triggered = False
        exit_price = None
        exit_reason = "time"
        bars_held = 0

        # Trailing stop peak/trough (same logic as TradingEngine)
        position_peak = entry_price
        ts_mult = strategy_params.get("trailing_stop_atr_mult", 0) if strategy_params else 0

        for step, (ts, bar) in enumerate(slice_df.iterrows()):
            bars_held = step + 1
            high_val = float(bar["high"])
            low_val = float(bar["low"])
            close_val = float(bar["close"])

            # Update trailing stop peaks/troughs
            if direction == "long":
                position_peak = max(position_peak, high_val)
            else:
                position_peak = min(position_peak, low_val)

            # Track MFE and MAE
            if direction == "long":
                if high_val > mfe_price:
                    mfe_price = high_val
                    mfe_time = ts.strftime("%H:%M:%S")
                if low_val < mae_price:
                    mae_price = low_val
                    mae_time = ts.strftime("%H:%M:%S")
            else:
                if low_val < mfe_price:
                    mfe_price = low_val
                    mfe_time = ts.strftime("%H:%M:%S")
                if high_val > mae_price:
                    mae_price = high_val
                    mae_time = ts.strftime("%H:%M:%S")

            # Update simulated position's current market price
            sim_pos.market_price = close_val

            # Check strategy-specific exit logic via on_bar
            strategy_exit = False
            if strategy_instance is not None:
                try:
                    sub_df = df.iloc[: entry_idx + step + 1]
                    from app.core.domain import Regime
                    regime = Regime.RANGE
                    if "regime" in bar:
                        regime = bar["regime"]
                    
                    sctx = StrategyContext(
                        position=sim_pos,
                        equity=100000.0,
                        regime=regime,
                        now=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                    )
                    exit_sig = strategy_instance.on_bar(sctx, sim_pos.symbol, sub_df)
                    if exit_sig is not None and exit_sig.action == SignalAction.EXIT:
                        strategy_exit = True
                except Exception:
                    pass

            if strategy_exit:
                exit_price = close_val
                exit_reason = "strategy_exit"
                break

            # Check trailing stop trigger (same logic as TradingEngine)
            trailing_stop_triggered = False
            if ts_mult > 0:
                atr = float(bar["atr14"]) if "atr14" in bar and pd.notna(bar["atr14"]) else 0.0
                if atr > 0:
                    if direction == "long":
                        stop_price = position_peak - ts_mult * atr
                        if close_val <= stop_price:
                            exit_price = stop_price
                            exit_reason = "trailing_stop"
                            trailing_stop_triggered = True
                    else:
                        stop_price = position_peak + ts_mult * atr
                        if close_val >= stop_price:
                            exit_price = stop_price
                            exit_reason = "trailing_stop"
                            trailing_stop_triggered = True

            if trailing_stop_triggered:
                break

            # Check standard bracket TP and SL triggers
            sl_hit = False
            tp_hit = False

            if direction == "long":
                if low_val <= initial_stop:
                    sl_hit = True
                if take_profit is not None and high_val >= take_profit:
                    tp_hit = True
            else:
                if high_val >= initial_stop:
                    sl_hit = True
                if take_profit is not None and low_val <= take_profit:
                    tp_hit = True

            # Resolve triggers (stop-loss wins ties)
            if sl_hit and tp_hit:
                sl_triggered = True
                exit_price = initial_stop
                exit_reason = "sl"
                if direction == "long":
                    mae_price = min(mae_price, initial_stop)
                else:
                    mae_price = max(mae_price, initial_stop)
                break
            elif sl_hit:
                sl_triggered = True
                exit_price = initial_stop
                exit_reason = "sl"
                if direction == "long":
                    mae_price = min(mae_price, initial_stop)
                else:
                    mae_price = max(mae_price, initial_stop)
                break
            elif tp_hit:
                tp_triggered = True
                exit_price = take_profit
                exit_reason = "tp"
                if direction == "long":
                    mfe_price = max(mfe_price, take_profit)
                else:
                    mfe_price = min(mfe_price, take_profit)
                break

        # Fallback if trade did not exit via TP/SL/trailing stop/strategy exit
        if exit_price is None:
            exit_price = float(slice_df["close"].iloc[-1])
            exit_reason = "time"

        # Excursion and returns calculation
        pnl = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        pnl_pct = (pnl / entry_price * 100.0) if entry_price > 0 else 0.0

        if direction == "long":
            mfe = mfe_price - entry_price
            mae = entry_price - mae_price
        else:
            mfe = entry_price - mfe_price
            mae = mae_price - entry_price

        mfe_pct = (mfe / entry_price * 100.0) if entry_price > 0 else 0.0
        mae_pct = (mae / entry_price * 100.0) if entry_price > 0 else 0.0

        return {
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "exit_reason": exit_reason,
            "bars_held": bars_held,
            "mfe_pct": round(mfe_pct, 4),
            "mae_pct": round(mae_pct, 4),
            "mfe_time": mfe_time or slice_df.index[0].strftime("%H:%M:%S"),
            "mae_time": mae_time or slice_df.index[0].strftime("%H:%M:%S"),
            "tp_hit": tp_triggered,
            "sl_hit": sl_triggered,
            "pnl_pct": round(pnl_pct, 4),
            "time_to_target_mins": bars_held if (tp_triggered or sl_triggered or exit_reason == "trailing_stop" or exit_reason == "strategy_exit") else None,
        }
