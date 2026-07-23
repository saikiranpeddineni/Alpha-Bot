"""Signal Shadow Ledger & Forward Outcome Tracker.
"""

from __future__ import annotations

import uuid
from datetime import datetime
import pandas as pd
import numpy as np

from app.core.logging import applog
from app.core.phase import classify_clock_phase, classify_market_behavior
from app.core.watchlist_mapper import OpportunityMapper, OpportunityClass

# Module-level persistence-failure counter for operational monitoring (Fix A9 / Issue #191).
_persistence_failure_count: int = 0


def get_persistence_failure_count() -> int:
    """Return the number of shadow-signal save failures since process start."""
    return _persistence_failure_count


def _classify_hybrid_effect(
    production_r: float,
    hybrid_r: float,
    epsilon: float = 1e-6,
) -> str:
    """Classify the research outcome of Hybrid BE relative to production."""
    delta = hybrid_r - production_r
    if abs(delta) <= epsilon:
        return "HYBRID_NO_EFFECT"
    if production_r < 0 and hybrid_r > production_r:
        return "HYBRID_SAVED_LOSS"
    if production_r > 0 and hybrid_r > production_r:
        return "HYBRID_IMPROVED_WINNER"
    if production_r > 0 and hybrid_r < production_r:
        return "HYBRID_REDUCED_WINNER"
    if production_r < 0 and hybrid_r < production_r:
        return "HYBRID_WORSENED_LOSS"
    return "HYBRID_OTHER"


class ShadowLedger:
    @staticmethod
    def calculate_forward_outcomes(
        df: pd.DataFrame,
        i: int,
        direction: str,
        stop_loss: float | None,
        entry_price: float,
        proposed_stop_loss: float | None = None,
    ) -> dict:
        """Calculate causal future returns, MAE, MFE, and R multiples at offsets

        1m, 5m, 15m, 30m, 60m, and session close.

        Fix #194/#197: Synthetic stop detection — when stop is missing/invalid, R-metrics
        are returned as None rather than computed from a fabricated risk distance.
        Prospective Telemetry Enhancement: When stop_loss is missing at candidate creation,
        proposed_stop_loss preserves the strategy's hypothetical risk definition.
        """
        outcomes = {}
        if i + 1 >= len(df):
            return outcomes

        stop_is_synthetic = False
        if stop_loss is None or stop_loss == entry_price:
            if proposed_stop_loss is not None and proposed_stop_loss != entry_price:
                initial_stop = proposed_stop_loss
                stop_is_synthetic = False
            else:
                initial_stop = entry_price * (0.985 if direction == "long" else 1.015)
                stop_is_synthetic = True
        else:
            initial_stop = stop_loss

        if direction == "long" and initial_stop >= entry_price:
            if proposed_stop_loss is not None and proposed_stop_loss < entry_price:
                initial_stop = proposed_stop_loss
                stop_is_synthetic = False
            else:
                initial_stop = entry_price * 0.985
                stop_is_synthetic = True
        elif direction == "short" and initial_stop <= entry_price:
            if proposed_stop_loss is not None and proposed_stop_loss > entry_price:
                initial_stop = proposed_stop_loss
                stop_is_synthetic = False
            else:
                initial_stop = entry_price * 1.015
                stop_is_synthetic = True

        initial_risk = abs(entry_price - initial_stop)
        initial_risk = max(initial_risk, 1e-8)

        offsets = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "60m": 60,
        }

        current_date = df.index[i].date()
        same_day_mask = df.index.date == current_date
        same_day_indices = np.where(same_day_mask)[0]
        close_idx = same_day_indices[-1] if len(same_day_indices) > 0 else len(df) - 1

        for label, offset in offsets.items():
            target_idx = i + 1 + offset
            if target_idx >= len(df):
                target_idx = len(df) - 1
            if target_idx > close_idx:
                target_idx = close_idx

            sub_df = df.iloc[i + 1 : target_idx + 1]
            if sub_df.empty:
                continue

            high_max = sub_df["high"].max()
            low_min = sub_df["low"].min()
            close_price = sub_df["close"].iloc[-1]

            stop_hit = False
            stop_bar_open = None
            for _, bar in sub_df.iterrows():
                if direction == "long" and bar["low"] <= initial_stop:
                    stop_hit = True
                    stop_bar_open = bar["open"]
                    break
                elif direction == "short" and bar["high"] >= initial_stop:
                    stop_hit = True
                    stop_bar_open = bar["open"]
                    break

            if direction == "long":
                mfe_high = min(high_max, stop_bar_open) if (stop_hit and stop_bar_open is not None) else high_max
                mfe = max(0.0, (mfe_high - entry_price) / entry_price * 100.0)
                mae = max(0.0, (entry_price - low_min) / entry_price * 100.0)
                pnl = (close_price - entry_price) / entry_price * 100.0
                dollar_pnl = close_price - entry_price
            else:
                mfe_low = max(low_min, stop_bar_open) if (stop_hit and stop_bar_open is not None) else low_min
                mfe = max(0.0, (entry_price - mfe_low) / entry_price * 100.0)
                mae = max(0.0, (high_max - entry_price) / entry_price * 100.0)
                pnl = (entry_price - close_price) / entry_price * 100.0
                dollar_pnl = entry_price - close_price

            if stop_is_synthetic:
                realized_r = None
                mfe_r = None
                mae_r = None
            else:
                realized_r = round(dollar_pnl / initial_risk, 4)
                mfe_r = round((mfe / 100.0 * entry_price) / initial_risk, 4)
                mae_r = round((mae / 100.0 * entry_price) / initial_risk, 4)

            outcomes[label] = {
                "pnl_pct": round(pnl, 4),
                "mfe_pct": round(mfe, 4),
                "mae_pct": round(mae, 4),
                "realized_r": realized_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "stop_hit": stop_hit,
                "stop_is_synthetic": stop_is_synthetic,
            }

        # Session close offset
        if close_idx > i + 1:
            close_sub = df.iloc[i + 1 : close_idx + 1]
            close_high = close_sub["high"].max()
            close_low = close_sub["low"].min()
            final_close = close_sub["close"].iloc[-1]

            close_stop_hit = False
            close_stop_open = None
            for _, bar in close_sub.iterrows():
                if direction == "long" and bar["low"] <= initial_stop:
                    close_stop_hit = True
                    close_stop_open = bar["open"]
                    break
                elif direction == "short" and bar["high"] >= initial_stop:
                    close_stop_hit = True
                    close_stop_open = bar["open"]
                    break

            if direction == "long":
                c_mfe_high = min(close_high, close_stop_open) if (close_stop_hit and close_stop_open is not None) else close_high
                c_mfe = max(0.0, (c_mfe_high - entry_price) / entry_price * 100.0)
                c_mae = max(0.0, (entry_price - close_low) / entry_price * 100.0)
                c_pnl = (final_close - entry_price) / entry_price * 100.0
                c_dollar_pnl = final_close - entry_price
            else:
                c_mfe_low = max(close_low, close_stop_open) if (close_stop_hit and close_stop_open is not None) else close_low
                c_mfe = max(0.0, (entry_price - c_mfe_low) / entry_price * 100.0)
                c_mae = max(0.0, (close_high - entry_price) / entry_price * 100.0)
                c_pnl = (entry_price - final_close) / entry_price * 100.0
                c_dollar_pnl = entry_price - final_close

            if stop_is_synthetic:
                c_realized_r = None
                c_mfe_r = None
                c_mae_r = None
            else:
                c_realized_r = round(c_dollar_pnl / initial_risk, 4)
                c_mfe_r = round((c_mfe / 100.0 * entry_price) / initial_risk, 4)
                c_mae_r = round((c_mae / 100.0 * entry_price) / initial_risk, 4)

            outcomes["close"] = {
                "pnl_pct": round(c_pnl, 4),
                "mfe_pct": round(c_mfe, 4),
                "mae_pct": round(c_mae, 4),
                "realized_r": c_realized_r,
                "mfe_r": c_mfe_r,
                "mae_r": c_mae_r,
                "stop_hit": close_stop_hit,
                "stop_is_synthetic": stop_is_synthetic,
            }

        return outcomes

    @staticmethod
    def record_signal(
        candidate_id: str,
        ts: datetime,
        symbol: str,
        strategy: str,
        direction: str,
        signal_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        clock_phase: str = "MIDDAY",
        behavior_state: str = "NORMAL",
        ml_confidence: float | None = None,
        final_decision: str = "rejected",
        rejection_stage: str = "none",
        rejection_reason: str = "",
        df: pd.DataFrame | None = None,
        db_engine: any = None,
        mode: str = "paper",
        policy_id: str = "champion",
        parent_policy_id: str | None = None,
        policy_version: str = "v1",
        gate_evidence_json: dict | None = None,
        spy_vol: float | None = None,
        market_regime: str = "RANGE",
        vix_value: float | None = None,
        vix_age: float | None = None,
        vix_feed_status: str = "FRESH",
        regime_source: str = "VIX",
        vwap_telemetry: dict | None = None,
        strategy_params: dict | None = None,
        alternative_exits: list[dict] | None = None,
        slot_rvol_val: float | None = None,
        proposed_stop_loss: float | None = None,
    ) -> dict:
        """Record candidate signal telemetry and forward price outcomes.

        Captures proposed risk parameters prior to ML gating to enable prospective counterfactual R evaluation.
        """
        if vwap_telemetry is None:
            vwap_telemetry = {}

        # Derive proposed stop loss if stop_loss was not explicitly passed
        if proposed_stop_loss is None:
            if stop_loss is not None:
                proposed_stop_loss = stop_loss
            elif strategy_params and isinstance(strategy_params, dict):
                sl_pct = strategy_params.get("stop_loss_pct") or strategy_params.get("sl_pct")
                if sl_pct and signal_price > 0:
                    proposed_stop_loss = signal_price * (1.0 - float(sl_pct) / 100.0 if direction == "long" else 1.0 + float(sl_pct) / 100.0)

        # Default strategy proposed stop loss (1.0% proposed risk distance) if unassigned
        if proposed_stop_loss is None and signal_price > 0:
            proposed_stop_loss = signal_price * (0.99 if direction == "long" else 1.01)

        prior_close = None
        open_price = None
        premarket_df = pd.DataFrame()

        if df is not None and not df.empty:
            ts_date = ts.date()
            day_bars = df[df.index.date == ts_date]
            if not day_bars.empty:
                prior_bars = df[df.index.date < ts_date]
                if not prior_bars.empty:
                    prior_close = float(prior_bars["close"].iloc[-1])
                premarket_df = day_bars[day_bars.index < day_bars.index[0].replace(hour=9, minute=30)]
                reg_bars = day_bars[day_bars.index >= day_bars.index[0].replace(hour=9, minute=30)]
                if not reg_bars.empty:
                    open_price = float(reg_bars["open"].iloc[0])

        opp_class = OpportunityMapper.classify_symbol(premarket_df, prior_close, open_price)

        entry_price = signal_price
        i = -1
        if df is not None and not df.empty:
            try:
                ts_lookup = ts
                from datetime import UTC
                if getattr(df.index, "tz", None) is None and ts_lookup.tzinfo is not None:
                    ts_lookup = ts_lookup.replace(tzinfo=None)
                elif getattr(df.index, "tz", None) is not None and ts_lookup.tzinfo is None:
                    ts_lookup = ts_lookup.replace(tzinfo=UTC)

                if ts_lookup in df.index:
                    i = df.index.get_loc(ts_lookup)
                else:
                    idx_loc = df.index.get_indexer([ts_lookup], method="nearest")[0]
                    if idx_loc >= 0:
                        i = int(idx_loc)
                if i + 1 >= len(df) and len(df) > 1:
                    i = max(0, len(df) - 30)
                if i >= 0 and i + 1 < len(df):
                    entry_price = float(df["open"].iloc[i + 1])
            except Exception:
                pass

        # Calculate outcomes using proposed_stop_loss when stop_loss is missing
        outcomes = {}
        if df is not None and not df.empty and i != -1:
            outcomes = ShadowLedger.calculate_forward_outcomes(
                df, i, direction, stop_loss, entry_price, proposed_stop_loss=proposed_stop_loss
            )

        payload = {
            "candidate_id": candidate_id,
            "timestamp": ts.isoformat(),
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,
            "signal_price": round(signal_price, 4),
            "entry_price": round(entry_price, 4),
            "stop_loss": round(stop_loss, 4) if stop_loss is not None else None,
            "proposed_stop_loss": round(proposed_stop_loss, 4) if proposed_stop_loss is not None else None,
            "take_profit": round(take_profit, 4) if take_profit is not None else None,
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
            "replay_valid": None,
            "replay_failure_reason": None,
            "simulated_pnl": None,
            "mfe_pct": None,
            "mae_pct": None,
            "exit_reason": None,
            "tp_hit": None,
            "sl_hit": None,
            "policy_id": policy_id,
            "parent_policy_id": parent_policy_id,
            "policy_version": policy_version,
            "gate_evidence_json": gate_evidence_json,
            "spy_vol": spy_vol,
            "regime": market_regime,
            "market_regime": market_regime,
            "vix_value": round(vix_value, 4) if vix_value is not None else None,
            "vix_age": round(vix_age, 2) if vix_age is not None else None,
            "vix_feed_status": vix_feed_status,
            "regime_source": regime_source,
        }

        # Calculate unique/shared/redundant veto attribution if gate_evidence_json is present
        if gate_evidence_json and "vetoes" in gate_evidence_json:
            vetoes = gate_evidence_json["vetoes"]
            from app.strategies.memory import _canonical_realized_r
            realized_r_val = _canonical_realized_r(strategy, outcomes) if outcomes else 0.0
            from app.strategies.evaluator import ParallelEvidenceEvaluator
            attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, realized_r_val)
            payload.update(attrib)

        # Run alternative exits if future data is present (backtest mode)
        if df is not None and not df.empty and i != -1 and outcomes:
            production_stop = float(stop_loss or proposed_stop_loss) if (stop_loss or proposed_stop_loss) is not None else None
            production_initial_risk = None
            if production_stop is not None:
                production_initial_risk = abs(float(entry_price) - production_stop)
                if production_initial_risk <= 1e-8:
                    production_initial_risk = None

            if alternative_exits is not None:
                alt_policies = alternative_exits
            else:
                from app.config import get_settings
                try:
                    settings = get_settings()
                    alt_policies = settings.risk_config().alternative_exits
                except Exception:
                    alt_policies = [
                        {"policy_id": "exit_bracket_tight", "take_profit_pct": 1.0, "stop_loss_pct": 0.5},
                        {"policy_id": "exit_bracket_wide", "take_profit_pct": 3.0, "stop_loss_pct": 1.5},
                        {"policy_id": "exit_atr_trailing", "trailing_stop_atr_mult": 2.0},
                        {"policy_id": "exit_hybrid_breakeven_atr", "breakeven_r_trigger": 0.30, "trail_r_trigger": 0.75, "tight_trail_r_trigger": 1.50},
                    ]

            alt_outcomes = {}
            prod_res = ShadowLedger.replay_signal_60m(
                df=df,
                entry_idx=i + 1,
                direction=direction,
                stop_loss=stop_loss or proposed_stop_loss,
                take_profit=take_profit,
                entry_price=entry_price,
                strategy_name=strategy,
                strategy_params=strategy_params,
            )
            if prod_res:
                prod_exit_p = prod_res.get("exit_price")
                prod_common_r = prod_res.get("realized_r")
                if production_initial_risk is not None and prod_exit_p is not None:
                    if direction == "long":
                        prod_common_r = (float(prod_exit_p) - float(entry_price)) / production_initial_risk
                    else:
                        prod_common_r = (float(entry_price) - float(prod_exit_p)) / production_initial_risk

                alt_outcomes["exit_production"] = {
                    "pnl_pct": prod_res["pnl_pct"],
                    "realized_r": prod_res.get("realized_r"),
                    "common_realized_r": round(prod_common_r, 6) if prod_common_r is not None else None,
                    "initial_risk_pct": round((production_initial_risk / float(entry_price) * 100.0), 4) if (entry_price and production_initial_risk) else None,
                    "exit_reason": prod_res["exit_reason"],
                    "tp_hit": prod_res["tp_hit"],
                    "sl_hit": prod_res["sl_hit"],
                    "bars_held": prod_res["bars_held"],
                    "mfe_r": prod_res.get("mfe_r"),
                    "mae_r": prod_res.get("mae_r"),
                    "stop_is_synthetic": prod_res.get("stop_is_synthetic", False),
                    "replay_valid": prod_res.get("replay_valid", True),
                }

            for alt in alt_policies:
                alt_policy_id = alt.get("policy_id")
                if not alt_policy_id:
                    continue

                alt_tp_val = None
                if "take_profit_pct" in alt:
                    alt_tp_val = entry_price * (1.0 + alt["take_profit_pct"] / 100.0 if direction == "long" else 1.0 - alt["take_profit_pct"] / 100.0)
                alt_sl_val = None
                if "stop_loss_pct" in alt:
                    alt_sl_val = entry_price * (1.0 - alt["stop_loss_pct"] / 100.0 if direction == "long" else 1.0 + alt["stop_loss_pct"] / 100.0)

                alt_params = {**strategy_params} if strategy_params else {}
                is_atr_trailing = "trailing_stop_atr_mult" in alt
                is_hybrid_exit = "breakeven_r_trigger" in alt
                if is_atr_trailing:
                    alt_params["trailing_stop_atr_mult"] = alt["trailing_stop_atr_mult"]
                if is_hybrid_exit:
                    alt_params["breakeven_r_trigger"] = alt.get("breakeven_r_trigger", 0.30)
                    alt_params["trail_r_trigger"] = alt.get("trail_r_trigger", 0.75)
                    alt_params["tight_trail_r_trigger"] = alt.get("tight_trail_r_trigger", 1.50)

                effective_tp = None if (is_atr_trailing or is_hybrid_exit) else (alt_tp_val or take_profit)

                alt_res = ShadowLedger.replay_signal_60m(
                    df=df,
                    entry_idx=i + 1,
                    direction=direction,
                    stop_loss=alt_sl_val or stop_loss or proposed_stop_loss,
                    take_profit=effective_tp,
                    entry_price=entry_price,
                    strategy_name=strategy,
                    strategy_params=alt_params,
                )
                if alt_res:
                    alt_exit_p = alt_res.get("exit_price")
                    common_realized_r = None
                    if production_initial_risk is not None and alt_exit_p is not None:
                        if direction == "long":
                            common_realized_r = (float(alt_exit_p) - float(entry_price)) / production_initial_risk
                        else:
                            common_realized_r = (float(entry_price) - float(alt_exit_p)) / production_initial_risk

                    eff_stop = alt_sl_val or stop_loss or proposed_stop_loss
                    alt_risk = abs(entry_price - eff_stop) if eff_stop is not None else None
                    alt_risk_pct = (alt_risk / float(entry_price) * 100.0) if (entry_price and alt_risk) else None

                    alt_outcomes[alt_policy_id] = {
                        "pnl_pct": alt_res["pnl_pct"],
                        "realized_r": alt_res.get("realized_r"),
                        "common_realized_r": round(common_realized_r, 6) if common_realized_r is not None else None,
                        "initial_risk_pct": round(alt_risk_pct, 4) if alt_risk_pct is not None else None,
                        "exit_reason": alt_res["exit_reason"],
                        "tp_hit": alt_res["tp_hit"],
                        "sl_hit": alt_res["sl_hit"],
                        "bars_held": alt_res["bars_held"],
                        "mfe_r": alt_res.get("mfe_r"),
                        "mae_r": alt_res.get("mae_r"),
                        "stop_is_synthetic": alt_res.get("stop_is_synthetic", False),
                        "replay_valid": alt_res.get("replay_valid", True),
                    }

            prod_common_r = alt_outcomes.get("exit_production", {}).get("common_realized_r")
            hybrid_outcome = alt_outcomes.get("exit_hybrid_breakeven_atr")
            if hybrid_outcome and prod_common_r is not None and hybrid_outcome.get("common_realized_r") is not None:
                h_r = hybrid_outcome["common_realized_r"]
                hybrid_delta_r = h_r - prod_common_r
                hybrid_outcome["delta_vs_production_r"] = round(hybrid_delta_r, 6)
                hybrid_outcome["effect"] = _classify_hybrid_effect(prod_common_r, h_r)

            payload["alternative_exits"] = alt_outcomes

        global _persistence_failure_count
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
            except Exception as exc:
                _persistence_failure_count += 1
                applog.error(
                    "shadow_ledger",
                    f"Failed to persist shadow signal candidate_id={candidate_id} "
                    f"symbol={symbol} strategy={strategy} ts={ts.isoformat()} — {exc}",
                )

        return payload
