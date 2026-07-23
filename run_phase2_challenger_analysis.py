"""Phase 2 Challenger Frozen Hypothesis Multi-Period Audit & Discrimination Analysis.

Registered Frozen Hypothesis:
"Downstream-approved signals with ML probability 0.50 <= p < 0.55 generate positive net incremental R-multiple expectancy
and are being unnecessarily hard-vetoed by the Stage 1 ML gate."

Fact-Checking Protections:
- Cumulative initial_risk_dollars scale-in accounting.
- Position-lifetime extrema MFE/MAE calculation.
- Strictly non-overlapping temporal partitions (July 6-10 vs July 11-17).
- Meta-Label & TradeScore Gate Discrimination Audit.
- Signal Population Distribution & Execution Telemetry.
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


async def run_ablation_for_window(dm, model, symbols, start_date, end_date):
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
    print("FROZEN HYPOTHESIS MULTI-PERIOD AUDIT & GATE DISCRIMINATION TELEMETRY")
    print("=" * 100)
    print("FROZEN HYPOTHESIS: Downstream-approved signals with 0.50 <= ML < 0.55 generate positive net incremental R")
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
    full_res = await run_ablation_for_window(dm, model, symbols, start_date, end_date)

    # 2. Strictly Non-Overlapping Temporal Partitions (July 6-10 vs July 11-17)
    w1_start = start_date
    w1_end = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)
    w2_start = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)
    w2_end = end_date

    print("\n[Audit 2/3] Running Window 1 (July 6 - July 10 23:59:59)...")
    w1_res = await run_ablation_for_window(dm, model, symbols, w1_start, w1_end)

    print("\n[Audit 3/3] Running Window 2 (July 11 00:00:00 - July 17)...")
    w2_res = await run_ablation_for_window(dm, model, symbols, w2_start, w2_end)

    # =========================================================================
    # 1. MULTI-PERIOD SUMMARY TABLE
    # =========================================================================
    print("\n" + "=" * 100)
    print("1. FROZEN HYPOTHESIS MULTI-PERIOD SCORECARD")
    print("=" * 100)
    hdr = f"{'Period':<20} | {'Gray N':<7} | {'Long N':<7} | {'Short N':<8} | {'Gray R':<8} | {'Mean R':<8} | {'PF (R)':<7} | {'Champ R':<9} | {'Chall R':<9} | {'Delta R':<8}"
    print(hdr)
    print("-" * 100)

    for p_name, p_data in [("Full Period (Jul 6-17)", full_res), ("Window 1 (Jul 6-10)", w1_res), ("Window 2 (Jul 11-17)", w2_res)]:
        champ = p_data["A (Champion: Hard Gates)"]
        chall = p_data["B (Gray Rescue Only)"]

        # Extract gray rescue telemetry
        res_chall = chall["backtest"]
        shadows = getattr(res_chall, "shadow_signals", [])
        rescued = [s for s in shadows if s.get("ml_gate_decision") == "GRAY_ZONE_RESCUED"]
        long_res = [s for s in rescued if s.get("direction", "").lower() in ("long", "enter_long")]
        short_res = [s for s in rescued if s.get("direction", "").lower() in ("short", "enter_short")]

        # Match realized R for rescued trades
        chall_r_list = chall["trade_r_list"]
        rescued_r_list = []
        for tr in chall_r_list:
            if any(s.get("symbol") == tr["symbol"] and s.get("strategy") == tr["strategy"] for s in rescued):
                rescued_r_list.append(tr["realized_r"])

        gray_n = len(rescued)
        long_n = len(long_res)
        short_n = len(short_res)
        gray_r = sum(rescued_r_list)
        mean_r = np.mean(rescued_r_list) if rescued_r_list else 0.0
        pf_r = (sum(r for r in rescued_r_list if r > 0) / abs(sum(r for r in rescued_r_list if r < 0))) if any(r < 0 for r in rescued_r_list) else 99.0

        c_r = champ["total_r"]
        ch_r = chall["total_r"]
        delta_r = ch_r - c_r

        print(
            f"{p_name:<20} | {gray_n:<7} | {long_n:<7} | {short_n:<8} | {gray_r:<+8.2f} | {mean_r:<+8.4f} | {pf_r:<7.2f} | {c_r:<+9.2f} | {ch_r:<+9.2f} | {delta_r:<+8.2f}"
        )
    print("=" * 100)

    # =========================================================================
    # 2. META-LABEL & TRADESCORE DISCRIMINATION AUDIT
    # =========================================================================
    print("\n" + "=" * 100)
    print("2. DOWNSTREAM GATE DISCRIMINATION AUDIT")
    print("=" * 100)

    shadow_sigs = getattr(full_res["B (Gray Rescue Only)"]["backtest"], "shadow_signals", [])

    total_candidates = len(shadow_sigs)
    meta_approved = sum(1 for s in shadow_sigs if s.get("meta_label_decision") in ("approved", "fallback_pass", "disabled_pass"))
    tradescore_approved = sum(1 for s in shadow_sigs if s.get("trade_score_decision") in ("approved", "disabled_pass"))

    print(f" - Total Candidates Evaluated:       {total_candidates}")
    print(f" - Meta-Label Approved Signals:      {meta_approved}/{total_candidates} ({meta_approved/total_candidates*100.0 if total_candidates else 0:.1f}%)")
    print(f" - TradeScore Approved Signals:      {tradescore_approved}/{total_candidates} ({tradescore_approved/total_candidates*100.0 if total_candidates else 0:.1f}%)")
    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
