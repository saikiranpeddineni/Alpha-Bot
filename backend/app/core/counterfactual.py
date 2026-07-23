"""Exit Counterfactual Engine for trade replay analysis.
"""

from __future__ import annotations

import pandas as pd
import numpy as np


class ExitCounterfactualEngine:
    @staticmethod
    def replay_policy(
        df: pd.DataFrame,
        entry_idx: int,
        entry_price: float,
        side: str,
        initial_stop: float,
        initial_atr: float,
        policy_name: str,
        policy_params: dict,
    ) -> dict:
        """Replay a single exit policy on the price path.

        Policy Params can specify:
          - stop_atr_mult
          - target_atr_mult
          - trailing_atr_mult
          - time_stop_bars
          - exit_on_ema_cross (bool)
        """
        # Enforce stop-loss sign sanity relative to entry price (gap protection)
        if side == "long" and initial_stop >= entry_price:
            initial_stop = entry_price * 0.985
        elif side == "short" and initial_stop <= entry_price:
            initial_stop = entry_price * 1.015

        initial_risk = abs(entry_price - initial_stop)
        # Prevent denominator inflation: minimum risk distance of 0.2% of entry or $0.05
        initial_risk = max(initial_risk, entry_price * 0.002, 0.05)

        # Extract path from entry_idx + 1 onwards (intraday only)
        current_date = df.index[entry_idx].date()
        path_df = df.iloc[entry_idx + 1 :]
        # Keep only bars of the same day
        path_df = path_df[path_df.index.date == current_date]

        if path_df.empty:
            return {
                "exit_price": entry_price,
                "exit_idx": entry_idx,
                "pnl": 0.0,
                "r_multiple": 0.0,
                "bars_held": 0,
                "ambiguous": False,
            }

        # Initialize policy variables
        stop_price = initial_stop
        target_price = None
        target_mult = policy_params.get("target_atr_mult")
        if target_mult:
            target_price = (
                entry_price + target_mult * initial_atr
                if side == "long"
                else entry_price - target_mult * initial_atr
            )

        stop_mult = policy_params.get("stop_atr_mult")
        if stop_mult:
            stop_price = (
                entry_price - stop_mult * initial_atr
                if side == "long"
                else entry_price + stop_mult * initial_atr
            )

        trailing_mult = policy_params.get("trailing_atr_mult")
        runner_trailing_mult = policy_params.get("runner_trailing_atr_mult")
        time_stop = policy_params.get("time_stop_bars")
        ema_cross = policy_params.get("exit_on_ema_cross")

        partial_target_r = policy_params.get("partial_target_r")
        partial_fraction = policy_params.get("partial_fraction", 0.50)
        partial_target_price = None
        if partial_target_r is not None:
            partial_target_price = (
                entry_price + partial_target_r * initial_risk
                if side == "long"
                else entry_price - partial_target_r * initial_risk
            )

        peak_price = entry_price
        ambiguous = False
        exit_price = None
        exit_idx = None
        bars_held = 0

        # Partial runner state tracking
        partial_taken = False
        realized_r_accum = 0.0
        remaining_fraction = 1.0

        for step, (ts, bar) in enumerate(path_df.iterrows()):
            bars_held = step + 1
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])

            # Check partial scaled exit target if enabled
            if partial_target_price and not partial_taken:
                partial_hit = (high >= partial_target_price) if side == "long" else (low <= partial_target_price)
                if partial_hit:
                    partial_taken = True
                    pnl_partial = (partial_target_price - entry_price) if side == "long" else (entry_price - partial_target_price)
                    r_partial = pnl_partial / initial_risk
                    realized_r_accum += partial_fraction * r_partial
                    remaining_fraction -= partial_fraction
                    # Move stop to breakeven after partial profit
                    stop_price = max(stop_price, entry_price) if side == "long" else min(stop_price, entry_price)

            # Time stop trigger
            if time_stop and bars_held >= time_stop:
                exit_price = close
                exit_idx = df.index.get_loc(ts)
                break

            # EMA cross trigger
            if ema_cross and "ema9" in bar and pd.notna(bar["ema9"]):
                ema9 = float(bar["ema9"])
                if side == "long" and close < ema9:
                    exit_price = close
                    exit_idx = df.index.get_loc(ts)
                    break
                elif side == "short" and close > ema9:
                    exit_price = close
                    exit_idx = df.index.get_loc(ts)
                    break

            # Update trailing stop (uses peak price reached)
            curr_trailing_mult = runner_trailing_mult if (partial_taken and runner_trailing_mult) else trailing_mult
            if curr_trailing_mult and "atr14" in bar and pd.notna(bar["atr14"]):
                curr_atr = float(bar["atr14"])
                if side == "long":
                    peak_price = max(peak_price, high)
                    proposed_stop = peak_price - curr_trailing_mult * curr_atr
                    stop_price = max(stop_price, proposed_stop)
                else:
                    peak_price = min(peak_price, low)
                    proposed_stop = peak_price + curr_trailing_mult * curr_atr
                    stop_price = min(stop_price, proposed_stop)

            # Check boundary conditions for stops and final targets
            stop_hit = False
            target_hit = False

            if side == "long":
                if low <= stop_price:
                    stop_hit = True
                if target_price and high >= target_price:
                    target_hit = True
            else:
                if high >= stop_price:
                    stop_hit = True
                if target_price and low <= target_price:
                    target_hit = True

            # Intrabar path ambiguity check
            if stop_hit and target_hit:
                ambiguous = True
                exit_price = stop_price
                exit_idx = df.index.get_loc(ts)
                break
            elif stop_hit:
                exit_price = stop_price
                exit_idx = df.index.get_loc(ts)
                break
            elif target_hit:
                exit_price = target_price
                exit_idx = df.index.get_loc(ts)
                break

        # Fallback to close of session if not triggered
        if exit_price is None:
            last_bar = path_df.iloc[-1]
            exit_price = float(last_bar["close"])
            exit_idx = df.index.get_loc(path_df.index[-1])

        # P&L and R computation (incorporates partial scaled exit if triggered)
        final_leg_pnl = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
        final_leg_r = final_leg_pnl / initial_risk
        
        if partial_taken:
            r_multiple = realized_r_accum + remaining_fraction * final_leg_r
            pnl = r_multiple * initial_risk
            effective_exit_price = (entry_price + pnl) if side == "long" else (entry_price - pnl)
        else:
            pnl = final_leg_pnl
            r_multiple = final_leg_r
            effective_exit_price = exit_price

        return {
            "exit_price": round(effective_exit_price, 4),
            "exit_idx": exit_idx,
            "pnl_pct": round(pnl / entry_price * 100.0, 4) if entry_price > 0 else 0.0,
            "r_multiple": round(r_multiple, 4),
            "bars_held": bars_held,
            "ambiguous": ambiguous,
        }

    @staticmethod
    def replay_all_policies(
        df: pd.DataFrame,
        entry_idx: int,
        entry_price: float,
        side: str,
        initial_stop: float,
        initial_atr: float,
    ) -> dict:
        """Run all default exit counterfactual policies for comparison."""
        policies = {
            "fixed_1_2": {"stop_atr_mult": 1.0, "target_atr_mult": 2.0},
            "fixed_1.5_3": {"stop_atr_mult": 1.5, "target_atr_mult": 3.0},
            "trail_2_atr": {"trailing_atr_mult": 2.0},
            "partial_1.5r_runner": {
                "partial_target_r": 1.5,
                "partial_fraction": 0.50,
                "runner_trailing_atr_mult": 2.5,
            },
            "time_30m": {"time_stop_bars": 30},
            "time_60m": {"time_stop_bars": 60},
            "ema9_cross": {"exit_on_ema_cross": True},
            "hold_to_close": {},  # no stop or target, exits at session close
        }

        results = {}
        for name, params in policies.items():
            results[name] = ExitCounterfactualEngine.replay_policy(
                df=df,
                entry_idx=entry_idx,
                entry_price=entry_price,
                side=side,
                initial_stop=initial_stop,
                initial_atr=initial_atr,
                policy_name=name,
                policy_params=params,
            )
        return results
