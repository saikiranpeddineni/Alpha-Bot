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
        if initial_stop == entry_price:
            raise ValueError("initial_stop must differ from entry_price")

        # Enforce stop-loss sign sanity relative to entry price (gap protection)
        if side == "long" and initial_stop > entry_price:
            initial_stop = entry_price * 0.985
        elif side == "short" and initial_stop < entry_price:
            initial_stop = entry_price * 1.015

        initial_risk = abs(entry_price - initial_stop)
        if initial_risk <= 0:
            raise ValueError("initial_stop must differ from entry_price")
        # Prevent denominator inflation: minimum risk distance of 0.2% of entry or $0.05
        initial_risk = max(initial_risk, entry_price * 0.002, 0.05)

        # Validate partial policy parameters if provided
        partial_target_r = policy_params.get("partial_target_r")
        partial_fraction = float(policy_params.get("partial_fraction", 0.50))
        runner_trailing_mult = policy_params.get("runner_trailing_atr_mult")

        if partial_target_r is not None:
            partial_target_r = float(partial_target_r)
            if partial_target_r <= 0:
                raise ValueError("partial_target_r must be > 0")
            if not 0.0 < partial_fraction < 1.0:
                raise ValueError("partial_fraction must satisfy 0 < fraction < 1")

        if runner_trailing_mult is not None:
            runner_trailing_mult = float(runner_trailing_mult)
            if runner_trailing_mult <= 0:
                raise ValueError("runner_trailing_atr_mult must be > 0")

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
                "partial_taken": False,
                "partial_fraction": 0.0,
                "runner_fraction": 0.0,
                "intrabar_ambiguous": False,
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

        # Partial runner state tracking with intrabar look-ahead protection
        partial_taken = False
        realized_r_accum = 0.0
        remaining_fraction = 1.0
        activate_runner_next_bar = False

        for step, (ts, bar) in enumerate(path_df.iterrows()):
            bars_held = step + 1
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])

            # Activate breakeven protection created by PREVIOUS bar's partial fill
            if activate_runner_next_bar:
                if side == "long":
                    stop_price = max(stop_price, entry_price)
                else:
                    stop_price = min(stop_price, entry_price)
                activate_runner_next_bar = False

            # Active stop entering this bar (cannot be modified retroactively by current bar)
            active_stop = stop_price
            next_bar_stop = active_stop

            old_stop_hit = (
                low <= active_stop if side == "long" else high >= active_stop
            )

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

            # Partial target check
            partial_hit = False
            if partial_target_price is not None and not partial_taken:
                partial_hit = (
                    high >= partial_target_price if side == "long" else low <= partial_target_price
                )

                if partial_hit and old_stop_hit:
                    ambiguous = True
                    # Conservative assumption: pre-existing stop hit first
                    exit_price = active_stop
                    exit_idx = df.index.get_loc(ts)
                    break

                if partial_hit:
                    partial_taken = True
                    r_partial = partial_target_r
                    realized_r_accum += partial_fraction * r_partial
                    remaining_fraction -= partial_fraction
                    activate_runner_next_bar = True

            # Update trailing stop for NEXT bar (Runner trailing does NOT activate on partial's own bar)
            curr_trailing_mult = trailing_mult
            if partial_taken and not activate_runner_next_bar:
                curr_trailing_mult = (
                    runner_trailing_mult if runner_trailing_mult is not None else trailing_mult
                )

            if curr_trailing_mult and "atr14" in bar and pd.notna(bar["atr14"]):
                curr_atr = float(bar["atr14"])
                if side == "long":
                    peak_price = max(peak_price, high)
                    proposed = peak_price - curr_trailing_mult * curr_atr
                    next_bar_stop = max(next_bar_stop, proposed)
                else:
                    peak_price = min(peak_price, low)
                    proposed = peak_price + curr_trailing_mult * curr_atr
                    next_bar_stop = min(next_bar_stop, proposed)

            # Check boundary conditions for stops and final targets
            stop_hit = old_stop_hit
            target_hit = False
            if target_price:
                target_hit = (high >= target_price) if side == "long" else (low <= target_price)

            if stop_hit and target_hit:
                ambiguous = True
                exit_price = active_stop
                exit_idx = df.index.get_loc(ts)
                break
            elif stop_hit:
                exit_price = active_stop
                exit_idx = df.index.get_loc(ts)
                break
            elif target_hit:
                exit_price = target_price
                exit_idx = df.index.get_loc(ts)
                break

            # Carry forward next bar stop protection
            stop_price = next_bar_stop

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
            "partial_taken": partial_taken,
            "partial_fraction": partial_fraction if partial_taken else 0.0,
            "runner_fraction": remaining_fraction if partial_taken else 0.0,
            "intrabar_ambiguous": ambiguous,
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
