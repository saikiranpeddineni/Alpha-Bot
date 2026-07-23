"""Phase 2 Challenger One-Factor Strategy & Gate Ablation Harness.

Rigorously Isolates 6 Independent One-Factor Experiments using Production Strategy Classes:
1. ML Gray-Zone Soft Rescue (ml_confidence_threshold=0.55, soft_composite_gating=False vs True).
2. Momentum Volume Z-score (vol_z_min=0.0 vs -0.5).
3. Momentum Event Window (event_window_bars=10 vs 15).
4. RSI2 Entry Threshold (rsi2_entry=5.0 vs 7.5).
5. VWAP Efficiency Ratio Max (er_max=0.30 vs 0.40).
6. VWAP Z-score Entry (z_entry=2.0 vs 1.5).

All experiments use exact candidate ID attribution and full backtest position/portfolio arbitration.
"""

import asyncio
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

from app.config import DATA_DIR, RiskConfig, get_settings
from app.data.manager import DataManager
from app.data.base import CachePolicy
from app.ai.model import SignalModel
from app.backtest.engine import BacktestParams, run_backtest
from app.strategies.base import REGISTRY
from run_profit_validation import discover_cache_bounds

ET = ZoneInfo("America/New_York")

class BlockingProvider:
    name = "blocking"
    async def get_bars(self, *a, **kw): raise RuntimeError("NETWORK BLOCKED")
    async def get_quotes(self, *a, **kw): return {}


def compute_trade_r(t):
    """Retrieve exact R, MFE, MAE from TradeRecord without synthetic fallbacks."""
    side = getattr(t, "side", "long" if t.qty > 0 else "short")
    entry_p = t.entry_price
    exit_p = t.exit_price

    init_stop = getattr(t, "initial_stop", None) or getattr(t, "stop_loss", None)
    init_risk = getattr(t, "initial_risk", None)

    if init_risk is None and init_stop is not None and init_stop != entry_p:
        init_risk = abs(entry_p - init_stop)

    r_valid = True
    if t.r_multiple is not None:
        realized_r = t.r_multiple
    elif init_risk is not None and init_risk > 0:
        pnl = (exit_p - entry_p) if side == "long" else (entry_p - exit_p)
        realized_r = pnl / init_risk
    else:
        realized_r = None
        r_valid = False

    mfe_val = getattr(t, "mfe", None)
    mae_val = getattr(t, "mae", None)
    mfe_r = (mfe_val / init_risk) if (mfe_val is not None and init_risk and init_risk > 0) else None
    mae_r = (mae_val / init_risk) if (mae_val is not None and init_risk and init_risk > 0) else None
    capture_eff = getattr(t, "capture_efficiency", None)

    return {
        "symbol": t.symbol,
        "strategy": t.strategy,
        "side": side,
        "entry_at": t.entry_at,
        "entry_price": entry_p,
        "stop_loss": init_stop,
        "exit_price": exit_p,
        "initial_risk": init_risk,
        "realized_r": round(realized_r, 4) if realized_r is not None else None,
        "r_valid": r_valid,
        "candidate_id": getattr(t, "candidate_id", None),
        "mfe_r": round(mfe_r, 4) if mfe_r is not None else None,
        "mae_r": round(mae_r, 4) if mae_r is not None else None,
        "capture_efficiency": round(capture_eff, 4) if capture_eff is not None else None,
        "pnl": round(t.pnl, 2),
        "exit_reason": getattr(t, "exit_reason", "normal"),
    }


async def run_single_experiment(dm, model, symbols, start_date, end_date, exp_name, champ_strats, champ_risk, chall_strats, chall_risk):
    params_champ = BacktestParams(
        symbols=symbols,
        start=start_date,
        end=end_date,
        starting_cash=100_000.0,
        slippage_bps=2.0,
        use_ml_gate=True,
        use_dynamic_scanner=True,
        strategies=champ_strats,
        risk=champ_risk,
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
        ignore_temporal_leakage=False,
    )
    res_c = await run_backtest(dm, model, params_champ)
    trades_c = [compute_trade_r(t) for t in res_c.trades if compute_trade_r(t)["r_valid"]]
    r_vals_c = [tr["realized_r"] for tr in trades_c]
    tot_r_c = sum(r_vals_c)

    params_chall = BacktestParams(
        symbols=symbols,
        start=start_date,
        end=end_date,
        starting_cash=100_000.0,
        slippage_bps=2.0,
        use_ml_gate=True,
        use_dynamic_scanner=True,
        strategies=chall_strats,
        risk=chall_risk,
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
        ignore_temporal_leakage=False,
    )
    res_ch = await run_backtest(dm, model, params_chall)
    trades_ch = [compute_trade_r(t) for t in res_ch.trades if compute_trade_r(t)["r_valid"]]
    r_vals_ch = [tr["realized_r"] for tr in trades_ch]
    tot_r_ch = sum(r_vals_ch)

    wins_ch = [r for r in r_vals_ch if r > 0]
    losses_ch = [abs(r) for r in r_vals_ch if r < 0]
    pf_ch = (sum(wins_ch) / sum(losses_ch)) if sum(losses_ch) > 0 else (99.0 if sum(wins_ch) > 0 else 0.0)
    win_rate_ch = (len(wins_ch) / len(r_vals_ch) * 100.0) if r_vals_ch else 0.0
    mean_r_ch = np.mean(r_vals_ch) if r_vals_ch else 0.0

    return {
        "exp_name": exp_name,
        "champ_trades": len(r_vals_c),
        "champ_r": round(tot_r_c, 2),
        "chall_trades": len(r_vals_ch),
        "chall_r": round(tot_r_ch, 2),
        "win_rate": round(win_rate_ch, 1),
        "mean_r": round(mean_r_ch, 4),
        "pf_r": round(pf_ch, 2),
        "delta_r": round(tot_r_ch - tot_r_c, 2),
    }


async def main():
    print("=" * 100)
    print("PHASE 2 CHALLENGER ONE-FACTOR FULL-SYSTEM ABLATION SCORECARD")
    print("=" * 100)

    cache_path = DATA_DIR / "cache.db"
    symbols, start_date, end_date = discover_cache_bounds(cache_path)

    dm = DataManager(BlockingProvider(), cache_path)
    dm.validate_cache_coverage = lambda *args, **kwargs: None

    models_dir = DATA_DIR / "models"
    model = SignalModel(models_dir)
    await model.load_artifacts()

    base_strats = {s_name: {"enabled": True, "params": {}} for s_name in REGISTRY.keys()}
    base_risk = RiskConfig(ml_confidence_threshold=0.55, soft_composite_gating=False)

    experiments = []

    # 1. ML Gray Rescue
    print("\n[Running Exp 1/6] ML Gray-Zone Soft Rescue (0.48 <= p < 0.55)...")
    chall_risk_1 = RiskConfig(ml_confidence_threshold=0.55, soft_composite_gating=True)
    exp1 = await run_single_experiment(dm, model, symbols, start_date, end_date, "1. ML Gray Rescue (0.48-0.55)", base_strats, base_risk, base_strats, chall_risk_1)
    experiments.append(exp1)

    # 2. Momentum Volume Z-score
    print("[Running Exp 2/6] Momentum Volume Z-score (vol_z_min: 0.0 -> -0.5)...")
    strats_2 = {s_name: {"enabled": True, "params": {}} for s_name in REGISTRY.keys()}
    strats_2["momentum"] = {"enabled": True, "params": {"vol_z_min": -0.5}}
    exp2 = await run_single_experiment(dm, model, symbols, start_date, end_date, "2. Momentum Vol Z (0.0 -> -0.5)", base_strats, base_risk, strats_2, base_risk)
    experiments.append(exp2)

    # 3. Momentum Event Window
    print("[Running Exp 3/6] Momentum Event Window (event_window_bars: 10 -> 15)...")
    strats_3 = {s_name: {"enabled": True, "params": {}} for s_name in REGISTRY.keys()}
    strats_3["momentum"] = {"enabled": True, "params": {"event_window_bars": 15}}
    exp3 = await run_single_experiment(dm, model, symbols, start_date, end_date, "3. Momentum Event Window (10 -> 15)", base_strats, base_risk, strats_3, base_risk)
    experiments.append(exp3)

    # 4. RSI2 Entry Threshold
    print("[Running Exp 4/6] RSI2 Entry Dip Threshold (rsi2_entry: 5.0 -> 7.5)...")
    strats_4 = {s_name: {"enabled": True, "params": {}} for s_name in REGISTRY.keys()}
    strats_4["rsi2_reversion"] = {"enabled": True, "params": {"rsi2_entry": 7.5}}
    exp4 = await run_single_experiment(dm, model, symbols, start_date, end_date, "4. RSI2 Dip Entry (5.0 -> 7.5)", base_strats, base_risk, strats_4, base_risk)
    experiments.append(exp4)

    # 5. VWAP ER Max
    print("[Running Exp 5/6] VWAP Efficiency Ratio Max (er_max: 0.30 -> 0.40)...")
    strats_5 = {s_name: {"enabled": True, "params": {}} for s_name in REGISTRY.keys()}
    strats_5["vwap_reversion"] = {"enabled": True, "params": {"er_max": 0.40}}
    exp5 = await run_single_experiment(dm, model, symbols, start_date, end_date, "5. VWAP ER Max (0.30 -> 0.40)", base_strats, base_risk, strats_5, base_risk)
    experiments.append(exp5)

    # 6. VWAP Z-score Entry
    print("[Running Exp 6/6] VWAP Z-score Entry (z_entry: 2.0 -> 1.5)...")
    strats_6 = {s_name: {"enabled": True, "params": {}} for s_name in REGISTRY.keys()}
    strats_6["vwap_reversion"] = {"enabled": True, "params": {"z_entry": 1.5}}
    exp6 = await run_single_experiment(dm, model, symbols, start_date, end_date, "6. VWAP Z Entry (2.0 -> 1.5)", base_strats, base_risk, strats_6, base_risk)
    experiments.append(exp6)

    # =========================================================================
    # ONE-FACTOR ABLATION SCORECARD TABLE
    # =========================================================================
    print("\n" + "=" * 100)
    print("ONE-FACTOR FULL-SYSTEM ABLATION SCORECARD (EXACT PRODUCTION STRATEGY CLASSES)")
    print("=" * 100)
    hdr = f"{'One-Factor Experiment':<32} | {'Champ N':<7} | {'Champ R':<8} | {'Chall N':<7} | {'Chall R':<8} | {'Win %':<6} | {'Mean R':<8} | {'PF (R)':<6} | {'Delta R':<8}"
    print(hdr)
    print("-" * 100)

    for exp in experiments:
        print(
            f"{exp['exp_name']:<32} | {exp['champ_trades']:<7} | {exp['champ_r']:<+8.2f} | {exp['chall_trades']:<7} | {exp['chall_r']:<+8.2f} | {exp['win_rate']:<6.1f} | {exp['mean_r']:<+8.4f} | {exp['pf_r']:<6.2f} | {exp['delta_r']:<+8.2f}"
        )

    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
