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
        time_stop = policy_params.get("time_stop_bars")
        ema_cross = policy_params.get("exit_on_ema_cross")

        peak_price = entry_price
        ambiguous = False
        exit_price = None
        exit_idx = None
        bars_held = 0

        # Close of day is the last bar in the path
        final_idx = len(path_df) - 1

        for step, (ts, bar) in enumerate(path_df.iterrows()):
            bars_held = step + 1
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            open_val = float(bar["open"])

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
            if trailing_mult and "atr14" in bar and pd.notna(bar["atr14"]):
                curr_atr = float(bar["atr14"])
                if side == "long":
                    peak_price = max(peak_price, high)
                    proposed_stop = peak_price - trailing_mult * curr_atr
                    stop_price = max(stop_price, proposed_stop)
                else:
                    peak_price = min(peak_price, low)
                    proposed_stop = peak_price + trailing_mult * curr_atr
                    stop_price = min(stop_price, proposed_stop)

            # Check boundary conditions for stops and targets
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
                # CONSERVATIVE: stop hit is assumed first
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

        # P&L and R computation
        pnl = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
        r_multiple = pnl / initial_risk

        return {
            "exit_price": round(exit_price, 4),
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
        """Run all 6 default exit counterfactual policies for comparison."""
        policies = {
            "fixed_1_2": {"stop_atr_mult": 1.0, "target_atr_mult": 2.0},
            "fixed_1.5_3": {"stop_atr_mult": 1.5, "target_atr_mult": 3.0},
            "trail_2_atr": {"trailing_atr_mult": 2.0},
            "partial_1.5r_runner": {"target_atr_mult": 1.5, "trailing_atr_mult": 2.5},
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
