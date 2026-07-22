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

        Fix #194/#197: Synthetic stop detection — when stop is missing/invalid, R-metrics
        are returned as None rather than computed from a fabricated risk distance.
        Fix #186: Session-close offset is inclusive of the last bar of the session.
        Fix #175/#183: MFE on a stop-hit bar is bounded by bar["open"] to prevent
        crediting favorable movement that occurred after the stop triggered.
        """
        outcomes = {}
        if i + 1 >= len(df):
            return outcomes

        # Fix A1 (#194/#197): Determine stop validity before computing risk.
        stop_is_synthetic = False
        if stop_loss is None or stop_loss == entry_price:
            initial_stop = entry_price * (0.985 if direction == "long" else 1.015)
            stop_is_synthetic = True
        else:
            initial_stop = stop_loss

        if direction == "long" and initial_stop >= entry_price:
            initial_stop = entry_price * 0.985
            stop_is_synthetic = True
        elif direction == "short" and initial_stop <= entry_price:
            initial_stop = entry_price * 1.015
            stop_is_synthetic = True

        initial_risk = abs(entry_price - initial_stop)
        # Epsilon guard only — prevents divide-by-zero; the old 0.2%/$0.05 floor
        # that corrupted valid tight stops is removed (Fix #197).
        initial_risk = max(initial_risk, 1e-8)

        # Offsets in bars (1 bar = 1 min)
        offsets = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "60m": 60,
        }

        # Fix A6 (#186): Session-close offset is now inclusive of the last bar.
        # Previously used close_idx - (i+1) which excluded the closing bar.
        current_date = df.index[i].date()
        day_bars = df[df.index.date == current_date]
        if not day_bars.empty:
            close_idx = df.index.get_loc(day_bars.index[-1])
            session_close_offset = max(1, close_idx - i)
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
                bar_open = float(bar["open"])

                if direction == "long":
                    if low_val <= initial_stop:
                        # Fix A2 (#175/#183): MFE bounded by bar open on stop-hit bar.
                        stop_hit = True
                        exit_price = initial_stop
                        mae_price = min(mae_price, initial_stop)
                        mfe_price = max(mfe_price, bar_open)
                        break
                    else:
                        mfe_price = max(mfe_price, high_val)
                        mae_price = min(mae_price, low_val)
                else:  # short
                    if high_val >= initial_stop:
                        # Fix A2 (#175/#183): MFE bounded by bar open on stop-hit bar.
                        stop_hit = True
                        exit_price = initial_stop
                        mae_price = max(mae_price, initial_stop)
                        mfe_price = min(mfe_price, bar_open)
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

            fwd_ret = pnl / entry_price if entry_price > 0 else 0.0

            # Fix A1 (#194/#197): Only emit R-metrics when stop is not synthetic.
            if not stop_is_synthetic:
                mfe_r = mfe / initial_risk
                mae_r = mae / initial_risk
                realized_r = pnl / initial_risk
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
                    "stop_is_synthetic": False,
                }
            else:
                outcomes[name] = {
                    "fwd_return": round(fwd_ret, 6),
                    "mfe": round(mfe, 4),
                    "mae": round(mae, 4),
                    "mfe_r": None,
                    "mae_r": None,
                    "realized_r": None,
                    "winner_capture_ratio": None,
                    "gave_back_r": None,
                    "gave_back_pct_of_mfe": None,
                    "mfe_before_loss_r": None,
                    "stop_is_synthetic": True,
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
        policy_id: str | None = None,
        parent_policy_id: str | None = None,
        policy_version: str | None = None,
        gate_evidence_json: dict | None = None,
        spy_vol: float | None = None,
        # Fix A5 (Issue #176): Accept runtime-tuned alternative exits from the engine.
        # When None, falls back to boot-time settings (backward-compatible).
        alternative_exits: list[dict] | None = None,
        market_regime: str | None = None,
        vix_value: float | None = None,
        vix_age: float | None = None,
        vix_feed_status: str | None = None,
        regime_source: str | None = None,
    ) -> dict:
        """Assembles a telemetry record, computes future outcomes, and saves it to the DB."""
        if candidate_id is None:
            # unique prefix: symbol_strat_date_hour_min_sec
            prefix = f"{symbol}_{strategy}_{ts.strftime('%Y%m%d_%H%M%S')}"
            candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()

        clock_phase = classify_clock_phase(ts)
        behavior_state = classify_market_behavior(spy_df, ts)

        # Track whether market_regime was supplied externally (VIX-derived) before we estimate
        _regime_externally_supplied = market_regime is not None

        if market_regime is None:
            # Estimate regime based on spy_vol if VIX is not provided (e.g. in backtests)
            if spy_vol is not None:
                if spy_vol > 0.15:
                    market_regime = "STRESS"
                elif spy_vol > 0.08:
                    market_regime = "CAUTION"
                else:
                    market_regime = "NORMAL"
            else:
                market_regime = "NORMAL"

        # Determine regime_source: explicit > caller-supplied > proxy
        if regime_source is None:
            if _regime_externally_supplied:
                regime_source = "VIX"
            elif spy_vol is not None:
                regime_source = "SPY_VOL_PROXY"
            else:
                regime_source = "UNKNOWN"

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
            "market_regime": market_regime,
            "vix_value": round(vix_value, 4) if vix_value is not None else None,
            "vix_age": round(vix_age, 2) if vix_age is not None else None,
            "vix_feed_status": vix_feed_status,
            "regime_source": regime_source,
        }

        # Calculate unique/shared/redundant veto attribution if gate_evidence_json is present
        if gate_evidence_json and "vetoes" in gate_evidence_json:
            vetoes = gate_evidence_json["vetoes"]
            # Fix C6 (Issue #173): Use canonical strategy horizon instead of hardcoded 15m.
            from app.strategies.memory import _canonical_realized_r
            realized_r_val = _canonical_realized_r(strategy, outcomes) if outcomes else 0.0
            from app.strategies.evaluator import ParallelEvidenceEvaluator
            attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, realized_r_val)
            payload.update(attrib)

        # Run alternative exits if future data is present (backtest mode)
        if df is not None and not df.empty and i != -1 and outcomes:
            # Fix A5 (Issue #176): Use runtime-tuned exits when provided by the engine.
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
                    ]

            alt_outcomes = {}
            # Run replay for production to get precise metrics
            prod_res = ShadowLedger.replay_signal_60m(
                df=df,
                entry_idx=i + 1,
                direction=direction,
                stop_loss=stop_loss,
                take_profit=take_profit,
                entry_price=entry_price,
                strategy_name=strategy,
                strategy_params=strategy_params,
            )
            if prod_res:
                alt_outcomes["exit_production"] = {
                    "pnl_pct": prod_res["pnl_pct"],
                    "realized_r": prod_res.get("realized_r"),
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

                # Fix A4 (Issue #179): ATR trailing & hybrid policies must NOT inherit the
                # production take-profit — they manage exits via the trailing mechanism.
                effective_tp = None if (is_atr_trailing or is_hybrid_exit) else (alt_tp_val or take_profit)

                alt_res = ShadowLedger.replay_signal_60m(
                    df=df,
                    entry_idx=i + 1,
                    direction=direction,
                    stop_loss=alt_sl_val or stop_loss,
                    take_profit=effective_tp,
                    entry_price=entry_price,
                    strategy_name=strategy,
                    strategy_params=alt_params,
                )
                if alt_res:
                    alt_outcomes[alt_policy_id] = {
                        "pnl_pct": alt_res["pnl_pct"],
                        "realized_r": alt_res.get("realized_r"),
                        "exit_reason": alt_res["exit_reason"],
                        "tp_hit": alt_res["tp_hit"],
                        "sl_hit": alt_res["sl_hit"],
                        "bars_held": alt_res["bars_held"],
                        "mfe_r": alt_res.get("mfe_r"),
                        "mae_r": alt_res.get("mae_r"),
                        "stop_is_synthetic": alt_res.get("stop_is_synthetic", False),
                        "replay_valid": alt_res.get("replay_valid", True),
                    }
            payload["alternative_exits"] = alt_outcomes

        # Fix A9 (Issue #191): Surface persistence failures instead of silently swallowing.
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

        Fixes applied:
          A1 (#194/#197): Synthetic stop detection; R-metrics null when stop invalid.
          A2 (#175/#183/#184): TP/SL checked BEFORE strategy on_bar; MFE/MAE bounded by open on exit bar.
          A3 (#180): Trailing stop evaluated using prior-bar peak (no same-bar look-ahead).
          A7 (#189): time_to_target_mins derived from timestamps, not bar count.
          A8 (#187): Strategy construction/on_bar failures surfaced via replay_valid flag.
        """
        if entry_idx >= len(df):
            return {}

        # Fix A1 (#194/#197): Stop validity detection.
        stop_is_synthetic = False
        if stop_loss is None or stop_loss == entry_price:
            initial_stop = entry_price * (0.985 if direction == "long" else 1.015)
            stop_is_synthetic = True
        else:
            initial_stop = stop_loss

        if direction == "long" and initial_stop >= entry_price:
            initial_stop = entry_price * 0.985
            stop_is_synthetic = True
        elif direction == "short" and initial_stop <= entry_price:
            initial_stop = entry_price * 1.015
            stop_is_synthetic = True

        # Path of the next 60 minutes capped at market session close (#231)
        slice_df = df.iloc[entry_idx : entry_idx + 60]
        if slice_df.empty:
            return {}

        entry_ts = slice_df.index[0]
        try:
            from app.core.calendar_helper import get_session_close, ET
            session_close_dt = get_session_close(entry_ts)
            if entry_ts.tzinfo is None:
                session_close_dt = session_close_dt.astimezone(ET).replace(tzinfo=None)
            if entry_ts <= session_close_dt:
                slice_df = slice_df[slice_df.index <= session_close_dt]
        except Exception:
            pass

        if slice_df.empty:
            return {}

        # Surface strategy construction failures (#230).
        strategy_instance = None
        replay_valid = True
        replay_failure_reason: str | None = None

        if strategy_name and strategy_params is not None:
            try:
                from app.strategies.base import REGISTRY
                strat_cls = REGISTRY.get(strategy_name)
                if strat_cls:
                    strategy_instance = strat_cls(params=strategy_params)
            except Exception as exc:
                replay_valid = False
                replay_failure_reason = f"strategy_construction_failed: {exc}"
                applog.warning(
                    "shadow_ledger",
                    f"replay_signal_60m: could not instantiate strategy '{strategy_name}': {exc}",
                )

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
        mfe_time: str | None = None
        mae_time: str | None = None
        mfe_step = 0
        mae_step = 0
        tp_triggered = False
        sl_triggered = False
        exit_price: float | None = None
        exit_reason = "time"
        bars_held = 0
        exit_ts: object = None

        # Fix A3 (#180): position_peak starts at entry_price (prior-bar state at bar 0).
        # We evaluate trailing-stop triggers BEFORE updating peak with the current bar.
        position_peak = entry_price
        initial_risk = max(abs(entry_price - initial_stop), 1e-8)
        ts_mult = strategy_params.get("trailing_stop_atr_mult", 0) if strategy_params else 0
        is_hybrid = ("breakeven_r_trigger" in strategy_params) if strategy_params else False
        be_trig = strategy_params.get("breakeven_r_trigger", 0.30) if strategy_params else 0.30
        trail_trig = strategy_params.get("trail_r_trigger", 0.75) if strategy_params else 0.75
        tight_trig = strategy_params.get("tight_trail_r_trigger", 1.50) if strategy_params else 1.50

        entry_ts = slice_df.index[0] if not slice_df.empty else None

        for step, (bar_ts, bar) in enumerate(slice_df.iterrows()):
            bars_held = step + 1
            high_val = float(bar["high"])
            low_val = float(bar["low"])
            close_val = float(bar["close"])
            bar_open = float(bar["open"])

            # Compute trailing stop or hybrid breakeven-ATR stop from prior-bar peak
            trailing_stop_price: float | None = None
            if ts_mult > 0:
                atr = float(bar["atr14"]) if "atr14" in bar and pd.notna(bar["atr14"]) else 0.0
                if atr > 0:
                    if direction == "long":
                        trailing_stop_price = position_peak - ts_mult * atr
                    else:
                        trailing_stop_price = position_peak + ts_mult * atr
            elif is_hybrid and initial_risk > 0:
                curr_mfe = (position_peak - entry_price) if direction == "long" else (entry_price - position_peak)
                curr_mfe_r = curr_mfe / initial_risk
                atr = float(bar["atr14"]) if "atr14" in bar and pd.notna(bar["atr14"]) else (entry_price * 0.01)
                eff_stop = initial_stop
                if curr_mfe_r >= be_trig:
                    be_price = entry_price * (1.0005 if direction == "long" else 0.9995)
                    eff_stop = max(eff_stop, be_price) if direction == "long" else min(eff_stop, be_price)
                if curr_mfe_r >= trail_trig:
                    mult = 1.0 if curr_mfe_r >= tight_trig else 1.5
                    t_price = (position_peak - mult * atr) if direction == "long" else (position_peak + mult * atr)
                    eff_stop = max(eff_stop, t_price) if direction == "long" else min(eff_stop, t_price)
                if eff_stop != initial_stop:
                    trailing_stop_price = eff_stop

            # Fix A2 (#184): Bracket TP/SL evaluated FIRST (resting broker orders fill intrabar).
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

            # Check trailing stop trigger (prior-bar level) if no bracket stop
            trailing_stop_triggered = False
            if trailing_stop_price is not None and not sl_hit:
                if direction == "long" and low_val <= trailing_stop_price:
                    trailing_stop_triggered = True
                elif direction == "short" and high_val >= trailing_stop_price:
                    trailing_stop_triggered = True

            # Resolve bracket triggers (ties: SL wins, marked ambiguous)
            if sl_hit and tp_hit:
                sl_triggered = True
                exit_price = initial_stop
                exit_reason = "ambiguous"
                exit_ts = bar_ts
                # Fix A2 (#175/#183): Bound MFE/MAE to bar open on exit bar
                if direction == "long":
                    mae_price = min(mae_price, initial_stop)
                    mfe_price = max(mfe_price, bar_open)
                else:
                    mae_price = max(mae_price, initial_stop)
                    mfe_price = min(mfe_price, bar_open)
                break
            elif sl_hit:
                sl_triggered = True
                exit_price = initial_stop
                exit_reason = "sl"
                exit_ts = bar_ts
                if direction == "long":
                    mae_price = min(mae_price, initial_stop)
                    mfe_price = max(mfe_price, bar_open)
                else:
                    mae_price = max(mae_price, initial_stop)
                    mfe_price = min(mfe_price, bar_open)
                break
            elif tp_hit:
                tp_triggered = True
                exit_price = take_profit
                exit_reason = "tp"
                exit_ts = bar_ts
                if direction == "long":
                    mfe_price = max(mfe_price, take_profit)  # type: ignore[arg-type]
                    mae_price = min(mae_price, bar_open)
                else:
                    mfe_price = min(mfe_price, take_profit)  # type: ignore[arg-type]
                    mae_price = max(mae_price, bar_open)
                break
            elif trailing_stop_triggered:
                exit_price = trailing_stop_price  # type: ignore[assignment]
                exit_reason = "trailing_stop"
                exit_ts = bar_ts
                if direction == "long":
                    mae_price = min(mae_price, trailing_stop_price)  # type: ignore[arg-type]
                    mfe_price = max(mfe_price, bar_open)
                else:
                    mae_price = max(mae_price, trailing_stop_price)  # type: ignore[arg-type]
                    mfe_price = min(mfe_price, bar_open)
                break

            # No bracket/trailing exit — update excursion metrics, then check close-based strategy exit.
            if direction == "long":
                if high_val > mfe_price:
                    mfe_price = high_val
                    mfe_time = bar_ts.strftime("%H:%M:%S")
                    mfe_step = step + 1
                if low_val < mae_price:
                    mae_price = low_val
                    mae_time = bar_ts.strftime("%H:%M:%S")
                    mae_step = step + 1
            else:
                if low_val < mfe_price:
                    mfe_price = low_val
                    mfe_time = bar_ts.strftime("%H:%M:%S")
                    mfe_step = step + 1
                if high_val > mae_price:
                    mae_price = high_val
                    mae_time = bar_ts.strftime("%H:%M:%S")
                    mae_step = step + 1

            # Update simulated position's current market price
            sim_pos.market_price = close_val

            # Fix A2 (#184): Strategy on_bar is close-based — after bracket checks.
            # Fix A8 (#187): Surface on_bar failures via replay_valid flag.
            strategy_exit = False
            if strategy_instance is not None and replay_valid:
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
                        now=bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts,
                    )
                    exit_sig = strategy_instance.on_bar(sctx, sim_pos.symbol, sub_df)
                    if exit_sig is not None and exit_sig.action == SignalAction.EXIT:
                        strategy_exit = True
                except Exception as exc:
                    replay_valid = False
                    replay_failure_reason = f"on_bar_failed: {exc}"
                    applog.warning(
                        "shadow_ledger",
                        f"replay_signal_60m: on_bar raised for '{strategy_name}' step={step}: {exc}",
                    )

            if strategy_exit:
                exit_price = close_val
                exit_reason = "strategy_exit"
                exit_ts = bar_ts
                break

            # Fix A3 (#180): Update peak AFTER trigger evaluation.
            if direction == "long":
                position_peak = max(position_peak, high_val)
            else:
                position_peak = min(position_peak, low_val)

        # (bracket/trailing check blocks moved above strategy on_bar — see fix A2/A3)

        # Fallback if trade did not exit via TP/SL/trailing stop/strategy exit
        if exit_price is None:
            exit_price = float(slice_df["close"].iloc[-1])
            exit_reason = "time"
            exit_ts = slice_df.index[-1]

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

        # Fix A7 (#189): time_to_target_mins derived from timestamps, not bar count.
        time_to_target_mins: float | None = None
        target_reached = tp_triggered or sl_triggered or exit_reason in ("trailing_stop", "strategy_exit")
        if target_reached and entry_ts is not None and exit_ts is not None:
            try:
                entry_dt = entry_ts.to_pydatetime() if hasattr(entry_ts, "to_pydatetime") else entry_ts
                exit_dt = exit_ts.to_pydatetime() if hasattr(exit_ts, "to_pydatetime") else exit_ts
                time_to_target_mins = round((exit_dt - entry_dt).total_seconds() / 60.0, 2)
            except Exception:
                time_to_target_mins = None

        exit_ts_str = None
        if exit_ts is not None:
            try:
                exit_ts_str = exit_ts.isoformat() if hasattr(exit_ts, "isoformat") else str(exit_ts)
            except Exception:
                exit_ts_str = str(exit_ts)

        # Fix A1 (#194/#197): Null R-metrics when stop is synthetic.
        if not stop_is_synthetic:
            initial_risk = abs(entry_price - initial_stop)
            initial_risk = max(initial_risk, 1e-8)

            mfe_r = mfe / initial_risk
            mae_r = mae / initial_risk
            realized_r = pnl / initial_risk
            winner_capture_ratio = realized_r / mfe_r if (realized_r > 0 and mfe_r > 0) else 0.0
            gave_back_r = mfe_r - realized_r
            gave_back_pct_of_mfe = (gave_back_r / mfe_r * 100.0) if mfe_r > 0 else 0.0
            mfe_before_loss_r = mfe_r if realized_r <= 0 else 0.0

            return {
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "exit_reason": exit_reason,
                "bars_held": bars_held,
                "exit_timestamp": exit_ts_str,
                "mfe_pct": round(mfe_pct, 4),
                "mae_pct": round(mae_pct, 4),
                "mfe_r": round(mfe_r, 4),
                "mae_r": round(mae_r, 4),
                "realized_r": round(realized_r, 4),
                "winner_capture_ratio": round(winner_capture_ratio, 4),
                "gave_back_r": round(gave_back_r, 4),
                "gave_back_pct_of_mfe": round(gave_back_pct_of_mfe, 2),
                "mfe_before_loss_r": round(mfe_before_loss_r, 4),
                "time_to_mfe": mfe_step,
                "time_to_mae": mae_step,
                "mfe_time": mfe_time or slice_df.index[0].strftime("%H:%M:%S"),
                "mae_time": mae_time or slice_df.index[0].strftime("%H:%M:%S"),
                "tp_hit": tp_triggered,
                "sl_hit": sl_triggered,
                "pnl_pct": round(pnl_pct, 4),
                "time_to_target_mins": time_to_target_mins,
                "stop_is_synthetic": False,
                "replay_valid": replay_valid,
                "replay_failed": not replay_valid,
                "replay_failure_reason": replay_failure_reason,
            }
        else:
            return {
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "exit_reason": exit_reason,
                "bars_held": bars_held,
                "exit_timestamp": exit_ts_str,
                "mfe_pct": round(mfe_pct, 4),
                "mae_pct": round(mae_pct, 4),
                "mfe_r": None,
                "mae_r": None,
                "realized_r": None,
                "winner_capture_ratio": None,
                "gave_back_r": None,
                "gave_back_pct_of_mfe": None,
                "mfe_before_loss_r": None,
                "time_to_mfe": mfe_step,
                "time_to_mae": mae_step,
                "mfe_time": mfe_time or slice_df.index[0].strftime("%H:%M:%S"),
                "mae_time": mae_time or slice_df.index[0].strftime("%H:%M:%S"),
                "tp_hit": tp_triggered,
                "sl_hit": sl_triggered,
                "pnl_pct": round(pnl_pct, 4),
                "time_to_target_mins": time_to_target_mins,
                "stop_is_synthetic": True,
                "replay_valid": replay_valid,
                "replay_failed": not replay_valid,
                "replay_failure_reason": replay_failure_reason,
            }
