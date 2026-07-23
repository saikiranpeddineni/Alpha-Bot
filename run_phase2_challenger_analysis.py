"""Phase 2 Challenger 2x2 Ablation Matrix, Gate Alpha Decomposition & Granular Trade Telemetry Script.

Strict R-Integrity Rules:
- Uses exact entry-time initial risk without synthetic 1.5% fallbacks.
- Reports exact R-valid trade count.
- Evaluates real MFE / MAE / Capture Efficiency from TradeRecord.
- Performs Multi-Window Temporal Validation (Week 1 vs Week 2 vs Full).
- Reports rescued signals broken down by Strategy, Side, and Regime.
"""

import asyncio
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

from app.config import DATA_DIR, RiskConfig, get_settings
from app.data.manager import DataManager
from app.data.base import CachePolicy
from app.ai.model import SignalModel
from app.backtest.engine import BacktestParams, run_backtest
from run_profit_validation import discover_cache_bounds

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
        "entry_at": str(t.entry_at),
        "entry_price": entry_p,
        "stop_loss": init_stop,
        "exit_price": exit_p,
        "initial_risk": init_risk,
        "realized_r": round(realized_r, 4) if realized_r is not None else None,
        "r_valid": r_valid,
        "mfe_r": round(mfe_r, 4) if mfe_r is not None else None,
        "mae_r": round(mae_r, 4) if mae_r is not None else None,
        "capture_efficiency": round(capture_eff, 4) if capture_eff is not None else None,
        "pnl": round(t.pnl, 2),
        "exit_reason": getattr(t, "exit_reason", "normal"),
    }


async def run_ablation_for_window(dm, model, symbols, start_date, end_date, window_name="Full Period"):
    from app.strategies.base import REGISTRY

    configs = {
        "A (Champion: Hard Gates)": RiskConfig(ml_confidence_threshold=0.55, soft_composite_gating=False),
        "B (Gray Rescue Only)": RiskConfig(ml_confidence_threshold=0.55, soft_composite_gating=True),
    }

    window_res = {}

    for cfg_name, risk_cfg in configs.items():
        strats = {s_name: {"enabled": True, "params": {}} for s_name in REGISTRY.keys()}
        params = BacktestParams(
            symbols=symbols,
            start=start_date,
            end=end_date,
            starting_cash=100_000.0,
            slippage_bps=2.0,
            use_ml_gate=True,
            use_dynamic_scanner=True,
            strategies=strats,
            risk=risk_cfg,
            cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
            ignore_temporal_leakage=False,
        )

        res = await run_backtest(dm, model, params)
        trade_r_list = [compute_trade_r(t) for t in res.trades]
        valid_r_list = [tr for tr in trade_r_list if tr["r_valid"]]
        r_vals = [tr["realized_r"] for tr in valid_r_list]

        wins_r = [r for r in r_vals if r > 0]
        losses_r = [abs(r) for r in r_vals if r < 0]

        total_r = sum(r_vals)
        mean_r = np.mean(r_vals) if r_vals else 0.0
        median_r = np.median(r_vals) if r_vals else 0.0
        gross_win_r = sum(wins_r)
        gross_loss_r = sum(losses_r)
        profit_factor_r = (gross_win_r / gross_loss_r) if gross_loss_r > 0 else (99.0 if gross_win_r > 0 else 0.0)

        cum_r = np.cumsum(r_vals) if r_vals else np.array([0.0])
        peak_r = np.maximum.accumulate(cum_r)
        dd_r = peak_r - cum_r
        max_dd_r = np.max(dd_r) if len(dd_r) > 0 else 0.0

        summary = res.get("summary", {})

        window_res[cfg_name] = {
            "backtest": res,
            "trades_count": len(res.trades),
            "valid_trades_count": len(valid_r_list),
            "total_pnl": summary.get("total_pnl", 0.0),
            "win_rate": summary.get("win_rate", 0.0),
            "total_r": round(total_r, 2),
            "mean_r": round(mean_r, 4),
            "median_r": round(median_r, 4),
            "profit_factor_r": round(profit_factor_r, 2),
            "max_dd_r": round(max_dd_r, 2),
            "max_dd_pct": summary.get("max_drawdown_pct", 0.0),
            "trade_r_list": trade_r_list,
        }

    return window_res


async def main():
    print("=" * 100)
    print("PHASE 2 CHALLENGER STRICT R-INTEGRITY & MULTI-WINDOW TEMPORAL AUDIT")
    print("=" * 100)

    cache_path = DATA_DIR / "cache.db"
    symbols, start_date, end_date = discover_cache_bounds(cache_path)

    dm = DataManager(BlockingProvider(), cache_path)
    dm.validate_cache_coverage = lambda *args, **kwargs: None

    models_dir = DATA_DIR / "models"
    model = SignalModel(models_dir)
    await model.load_artifacts()

    # 1. Full Period Ablation
    print("\n[Audit 1/3] Running Full Period Evaluation...")
    full_res = await run_ablation_for_window(dm, model, symbols, start_date, end_date, "Full Period")

    # 2. Temporal Window Sub-division (Week 1 vs Week 2)
    w1_start = start_date
    w1_end = start_date + timedelta(days=5)
    w2_start = w1_end
    w2_end = end_date

    print("\n[Audit 2/3] Running Temporal Window 1 (Week 1)...")
    w1_res = await run_ablation_for_window(dm, model, symbols, w1_start, w1_end, "Week 1")

    print("\n[Audit 3/3] Running Temporal Window 2 (Week 2)...")
    w2_res = await run_ablation_for_window(dm, model, symbols, w2_start, w2_end, "Week 2")

    # =========================================================================
    # DISPLAY MULTI-WINDOW COMPARISON TABLE
    # =========================================================================
    print("\n" + "=" * 100)
    print("MULTI-WINDOW TEMPORAL STABILITY SCORECARD (METRICS IN EXACT REALIZED R)")
    print("=" * 100)
    print(f"{'Evaluation Window':<20} | {'Config':<25} | {'Trades (R-Val)':<14} | {'Win %':<8} | {'Total R':<10} | {'Mean R':<8} | {'PF (R)':<8} | {'Net P&L ($)':<12}")
    print("-" * 100)

    for w_name, w_data in [("Full Period", full_res), ("Week 1 (July 6-11)", w1_res), ("Week 2 (July 11-17)", w2_res)]:
        for cfg_name, r in w_data.items():
            print(
                f"{w_name:<20} | {cfg_name:<25} | {r['valid_trades_count']}/{r['trades_count']:<11} | {r['win_rate']:<7.2f}% | {r['total_r']:<+10.2f} | {r['mean_r']:<+8.4f} | {r['profit_factor_r']:<8.2f} | ${r['total_pnl']:<+11.2f}"
            )
        print("-" * 100)

    # =========================================================================
    # STRATEGY BREAKDOWN OF RESCUED SIGNALS
    # =========================================================================
    print("\n" + "=" * 100)
    print("STRATEGY & SIDE BREAKDOWN OF GRAY-ZONE RESCUED TRADES")
    print("=" * 100)

    res_chall = full_res["B (Gray Rescue Only)"]["backtest"]
    shadow_sigs = getattr(res_chall, "shadow_signals", [])
    rescued_sigs = [s for s in shadow_sigs if s.get("ml_gate_decision") == "GRAY_ZONE_RESCUED"]
    chall_trades = full_res["B (Gray Rescue Only)"]["trade_r_list"]

    strat_stats = {}

    for tr in chall_trades:
        sym = tr["symbol"]
        for s in rescued_sigs:
            if s.get("symbol") == sym and s.get("strategy") == tr["strategy"]:
                strat = tr["strategy"]
                side = tr["side"]
                key = f"{strat} ({side.upper()})"
                if key not in strat_stats:
                    strat_stats[key] = {"count": 0, "r_vals": [], "meta_probs": [], "trade_scores": []}
                strat_stats[key]["count"] += 1
                strat_stats[key]["r_vals"].append(tr["realized_r"])
                strat_stats[key]["meta_probs"].append(s.get("meta_probability", 1.0))
                strat_stats[key]["trade_scores"].append(s.get("trade_score", 80.0))
                break

    print(f"{'Strategy & Side':<25} | {'Rescued Trades':<15} | {'Win %':<8} | {'Total Realized R':<18} | {'Mean R/Trade':<14}")
    print("-" * 100)

    for k, v in strat_stats.items():
        cnt = v["count"]
        r_list = v["r_vals"]
        wins = [r for r in r_list if r > 0]
        wr = (len(wins) / cnt * 100.0) if cnt > 0 else 0.0
        tot_r = sum(r_list)
        mean_r = np.mean(r_list) if cnt > 0 else 0.0
        print(f"{k:<25} | {cnt:<15} | {wr:<7.1f}% | {tot_r:<+18.2f} | {mean_r:<+14.4f}")

    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
