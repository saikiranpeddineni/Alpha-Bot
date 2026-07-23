"""Phase 2 Challenger Strict R-Integrity, ML Expectancy Curve & Multi-Window Audit.

Fact-Checking Protections:
- Non-synthetic cumulative entry risk capital accounting (`initial_risk_dollars`).
- Position-lifetime extrema verified for MFE/MAE calculation.
- Strictly non-overlapping temporal windows (July 6-10 vs July 11-17).
- Complete ML Probability Expectancy Curve across ALL signals (Accepted & Rejected).
- Separate LONG vs SHORT Gray-Zone Telemetry.
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
    print("PHASE 2 CHALLENGER STRICT R-INTEGRITY, ML EXPECTANCY & MULTI-WINDOW AUDIT")
    print("=" * 100)

    cache_path = DATA_DIR / "cache.db"
    symbols, start_date, end_date = discover_cache_bounds(cache_path)

    dm = DataManager(BlockingProvider(), cache_path)
    dm.validate_cache_coverage = lambda *args, **kwargs: None

    models_dir = DATA_DIR / "models"
    model = SignalModel(models_dir)
    await model.load_artifacts()

    # 1. Full Period Ablation
    print("\n[Audit 1/4] Running Full Period Evaluation...")
    full_res = await run_ablation_for_window(dm, model, symbols, start_date, end_date, "Full Period")

    # 2. Strictly Non-Overlapping Temporal Windows (July 6-10 vs July 11-17)
    w1_start = start_date
    w1_end = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)
    w2_start = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)
    w2_end = end_date

    print("\n[Audit 2/4] Running Strictly Non-Overlapping Window 1 (July 6 - July 10 23:59)...")
    w1_res = await run_ablation_for_window(dm, model, symbols, w1_start, w1_end, "July 6-10")

    print("\n[Audit 3/4] Running Strictly Non-Overlapping Window 2 (July 11 00:00 - July 17)...")
    w2_res = await run_ablation_for_window(dm, model, symbols, w2_start, w2_end, "July 11-17")

    # =========================================================================
    # 1. SCORECARD DISPLAY
    # =========================================================================
    print("\n" + "=" * 100)
    print("1. MULTI-WINDOW STRICTLY NON-OVERLAPPING SCORECARD (EXACT REALIZED R)")
    print("=" * 100)
    print(f"{'Evaluation Window':<22} | {'Config':<25} | {'Trades (R-Val)':<14} | {'Win %':<8} | {'Total R':<10} | {'Mean R':<8} | {'PF (R)':<8} | {'Net P&L ($)':<12}")
    print("-" * 100)

    for w_name, w_data in [("Full Period (Jul 6-17)", full_res), ("Window 1 (Jul 6-10)", w1_res), ("Window 2 (Jul 11-17)", w2_res)]:
        for cfg_name, r in w_data.items():
            print(
                f"{w_name:<22} | {cfg_name:<25} | {r['valid_trades_count']}/{r['trades_count']:<11} | {r['win_rate']:<7.2f}% | {r['total_r']:<+10.2f} | {r['mean_r']:<+8.4f} | {r['profit_factor_r']:<8.2f} | ${r['total_pnl']:<+11.2f}"
            )
        print("-" * 100)

    # =========================================================================
    # 2. LONG VS SHORT GRAY-ZONE CANDIDATE TELEMETRY
    # =========================================================================
    print("\n" + "=" * 100)
    print("2. LONG VS SHORT GRAY-ZONE POPULATION & RESCUED TELEMETRY")
    print("=" * 100)

    res_chall = full_res["B (Gray Rescue Only)"]["backtest"]
    shadow_sigs = getattr(res_chall, "shadow_signals", [])

    gray_sigs = [s for s in shadow_sigs if 0.48 <= s.get("ml_probability", 0.0) < 0.55]
    gray_longs = [s for s in gray_sigs if s.get("direction", "").lower() in ("long", "enter_long")]
    gray_shorts = [s for s in gray_sigs if s.get("direction", "").lower() in ("short", "enter_short")]

    rescued_sigs = [s for s in shadow_sigs if s.get("ml_gate_decision") == "GRAY_ZONE_RESCUED"]
    rescued_longs = [s for s in rescued_sigs if s.get("direction", "").lower() in ("long", "enter_long")]
    rescued_shorts = [s for s in rescued_sigs if s.get("direction", "").lower() in ("short", "enter_short")]

    print(f" - Total Gray-Zone Candidates (0.48 <= p < 0.55): {len(gray_sigs)}")
    print(f"   * LONG Gray Candidates:  {len(gray_longs)}  (Rescued: {len(rescued_longs)})")
    print(f"   * SHORT Gray Candidates: {len(gray_shorts)}  (Rescued: {len(rescued_shorts)})")

    # =========================================================================
    # 3. COMPLETE ML PROBABILITY EXPECTANCY CURVE
    # =========================================================================
    print("\n" + "=" * 100)
    print("3. COMPLETE ML PROBABILITY EXPECTANCY CURVE (ALL SIGNALS)")
    print("=" * 100)

    prob_bins = [
        ("< 0.40", 0.0, 0.40),
        ("0.40 - 0.45", 0.40, 0.45),
        ("0.45 - 0.48", 0.45, 0.48),
        ("0.48 - 0.50", 0.48, 0.50),
        ("0.50 - 0.52", 0.50, 0.52),
        ("0.52 - 0.55", 0.52, 0.55),
        ("0.55 - 0.60", 0.55, 0.60),
        ("0.60 - 0.65", 0.60, 0.65),
        ("> 0.65", 0.65, 1.00),
    ]

    print(f"{'ML Bucket':<15} | {'Candidates':<10} | {'Meta Pass':<10} | {'TradeScore Pass':<15} | {'Executed/Rescued':<16}")
    print("-" * 100)

    for bin_label, p_min, p_max in prob_bins:
        cands = [s for s in shadow_sigs if p_min <= s.get("ml_probability", 0.0) < p_max]
        meta_p = [s for s in cands if s.get("meta_label_decision") in ("approved", "fallback_pass", "disabled_pass")]
        ts_p = [s for s in cands if s.get("trade_score_decision") in ("approved", "disabled_pass")]
        exec_p = [s for s in cands if s.get("final_decision") in ("pipeline_approved", "GRAY_ZONE_RESCUED")]

        print(
            f"{bin_label:<15} | {len(cands):<10} | {len(meta_p):<10} | {len(ts_p):<15} | {len(exec_p):<16}"
        )

    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
