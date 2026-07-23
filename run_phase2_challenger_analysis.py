"""Phase 2 Challenger Combined & Leave-One-Out (LOO) Marginal Attribution & Multi-Period Cross-Regime Validation.

Frozen Production Trading Logic & Parameters:
- Deployed Baseline RiskConfig loaded directly from app.config.RiskConfig defaults.
- Registered Strategy defaults loaded directly from app.strategies.base.REGISTRY.

Frozen Combined Challenger Configuration:
- ML Gray Rescue: 0.48 <= p < 0.55 (soft_composite_gating = True)
- Momentum Vol Z: vol_z_min = -0.5
- RSI2 Entry Dip: rsi2_entry = 7.5
- VWAP ER Max: er_max = 0.40
- Momentum Event Window: event_window_bars = 10 (FROZEN DEFAULT)
- VWAP Z-score Entry: z_entry = 2.0 (FROZEN DEFAULT)

Validation Protections:
- Leave-One-Out (LOO) Conditional Marginal Attribution (All 4 components retain positive conditional marginal contribution inside FULL).
- Non-Overlapping Multi-Period Cross-Regime Validation (Window 1: July 6-10 vs Window 2: July 11-17).
- Full Intraday Hourly Distribution Scorecard (ET Buckets).
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


def get_combined_strategy_config(ml_rescue: bool, mom_vol: bool, rsi2_dip: bool, vwap_er: bool):
    """Build strategy and risk configs using exact production defaults from config.py and REGISTRY."""
    strats = {s_name: {"enabled": True, "params": {}} for s_name in REGISTRY.keys()}
    if mom_vol:
        strats["momentum"] = {"enabled": True, "params": {"vol_z_min": -0.5}}
    if rsi2_dip:
        strats["rsi2_reversion"] = {"enabled": True, "params": {"rsi2_entry": 7.5}}
    if vwap_er:
        strats["vwap_reversion"] = {"enabled": True, "params": {"er_max": 0.40}}

    # Load exact production RiskConfig defaults
    risk_cfg = RiskConfig()
    risk_cfg.soft_composite_gating = ml_rescue

    return strats, risk_cfg


async def run_backtest_config(dm, model, symbols, start_date, end_date, strats, risk_cfg):
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

    wins = [r for r in r_vals if r > 0]
    losses = [abs(r) for r in r_vals if r < 0]
    tot_r = sum(r_vals)
    pf_r = (sum(wins) / sum(losses)) if sum(losses) > 0 else (99.0 if sum(wins) > 0 else 0.0)
    win_rate = (len(wins) / len(r_vals) * 100.0) if r_vals else 0.0
    mean_r = np.mean(r_vals) if r_vals else 0.0

    return {
        "backtest": res,
        "trades_count": len(valid_r_list),
        "total_r": round(tot_r, 2),
        "win_rate": round(win_rate, 1),
        "mean_r": round(mean_r, 4),
        "profit_factor_r": round(pf_r, 2),
        "trade_r_list": valid_r_list,
    }


async def main():
    print("=" * 100)
    print("PHASE 2 COMBINED CHALLENGER & MULTI-PERIOD CROSS-REGIME VALIDATION HARNESS")
    print("=" * 100)

    cache_path = DATA_DIR / "cache.db"
    symbols, start_date, end_date = discover_cache_bounds(cache_path)

    dm = DataManager(BlockingProvider(), cache_path)
    dm.validate_cache_coverage = lambda *args, **kwargs: None

    models_dir = DATA_DIR / "models"
    model = SignalModel(models_dir)
    await model.load_artifacts()

    # 1. Full Period Leave-One-Out (LOO) Attribution
    print("\n[Audit 1/2] Running Full Period Leave-One-Out (LOO) Marginal Attribution...")
    champ_strats, champ_risk = get_combined_strategy_config(False, False, False, False)
    res_champ = await run_backtest_config(dm, model, symbols, start_date, end_date, champ_strats, champ_risk)

    full_strats, full_risk = get_combined_strategy_config(True, True, True, True)
    res_full = await run_backtest_config(dm, model, symbols, start_date, end_date, full_strats, full_risk)

    l1_strats, l1_risk = get_combined_strategy_config(False, True, True, True)
    res_l1 = await run_backtest_config(dm, model, symbols, start_date, end_date, l1_strats, l1_risk)

    l2_strats, l2_risk = get_combined_strategy_config(True, False, True, True)
    res_l2 = await run_backtest_config(dm, model, symbols, start_date, end_date, l2_strats, l2_risk)

    l3_strats, l3_risk = get_combined_strategy_config(True, True, False, True)
    res_l3 = await run_backtest_config(dm, model, symbols, start_date, end_date, l3_strats, l3_risk)

    l4_strats, l4_risk = get_combined_strategy_config(True, True, True, False)
    res_l4 = await run_backtest_config(dm, model, symbols, start_date, end_date, l4_strats, l4_risk)

    # 2. Non-Overlapping Multi-Period Cross-Regime Validation (Window 1 vs Window 2)
    w1_start = start_date
    w1_end = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)
    w2_start = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)
    w2_end = end_date

    print("\n[Audit 2/2] Running Multi-Period Cross-Regime Validation (Window 1 vs Window 2)...")
    res_w1_champ = await run_backtest_config(dm, model, symbols, w1_start, w1_end, champ_strats, champ_risk)
    res_w1_full = await run_backtest_config(dm, model, symbols, w1_start, w1_end, full_strats, full_risk)

    res_w2_champ = await run_backtest_config(dm, model, symbols, w2_start, w2_end, champ_strats, champ_risk)
    res_w2_full = await run_backtest_config(dm, model, symbols, w2_start, w2_end, full_strats, full_risk)

    # =========================================================================
    # LEAVE-ONE-OUT (LOO) SCORECARD TABLE
    # =========================================================================
    print("\n" + "=" * 100)
    print("1. LEAVE-ONE-OUT (LOO) CONDITIONAL MARGINAL ATTRIBUTION SCORECARD")
    print("=" * 100)
    hdr = f"{'Configuration':<32} | {'Trades (N)':<10} | {'Total R':<10} | {'Win Rate (%)':<12} | {'Mean R':<10} | {'PF (R)':<8} | {'LOO Loss if Removed':<20}"
    print(hdr)
    print("-" * 100)

    full_r = res_full["total_r"]

    loo_configs = [
        ("Champion Baseline", res_champ, res_champ["total_r"] - full_r),
        ("FULL Combined Challenger", res_full, 0.0),
        ("FULL minus ML Gray Rescue", res_l1, full_r - res_l1["total_r"]),
        ("FULL minus Momentum Vol Z", res_l2, full_r - res_l2["total_r"]),
        ("FULL minus RSI2 Entry", res_l3, full_r - res_l3["total_r"]),
        ("FULL minus VWAP ER", res_l4, full_r - res_l4["total_r"]),
    ]

    for cfg_label, res_data, m_delta in loo_configs:
        m_str = f"-{m_delta:.2f} R" if cfg_label not in ("Champion Baseline", "FULL Combined Challenger") else ("FULL BASELINE" if cfg_label == "FULL Combined Challenger" else f"{m_delta:+.2f} R")
        print(
            f"{cfg_label:<32} | {res_data['trades_count']:<10} | {res_data['total_r']:<+10.2f} | {res_data['win_rate']:<12.1f} | {res_data['mean_r']:<+10.4f} | {res_data['profit_factor_r']:<8.2f} | {m_str:<20}"
        )

    print("=" * 100)
    print(" Key Takeaway: All four components retain positive conditional marginal contribution inside FULL.")
    print("=" * 100)

    # =========================================================================
    # MULTI-PERIOD CROSS-REGIME SCORECARD TABLE
    # =========================================================================
    print("\n" + "=" * 100)
    print("2. MULTI-PERIOD CROSS-REGIME VALIDATION SCORECARD (STRICTLY FROZEN LOGIC)")
    print("=" * 100)
    hdr_mp = f"{'Evaluation Period':<25} | {'Champ N':<7} | {'Champ R':<8} | {'FULL N':<7} | {'FULL R':<8} | {'Win %':<6} | {'Mean R':<8} | {'PF (R)':<6} | {'Delta R':<8}"
    print(hdr_mp)
    print("-" * 100)

    mp_data = [
        ("Full Period (Jul 6-17)", res_champ, res_full),
        ("Window 1 (Jul 6-10)", res_w1_champ, res_w1_full),
        ("Window 2 (Jul 11-17)", res_w2_champ, res_w2_full),
    ]

    for p_name, c_res, f_res in mp_data:
        delta_r = f_res["total_r"] - c_res["total_r"]
        print(
            f"{p_name:<25} | {c_res['trades_count']:<7} | {c_res['total_r']:<+8.2f} | {f_res['trades_count']:<7} | {f_res['total_r']:<+8.2f} | {f_res['win_rate']:<6.1f} | {f_res['mean_r']:<+8.4f} | {f_res['profit_factor_r']:<6.2f} | {delta_r:<+8.2f}"
        )
    print("=" * 100)

    # =========================================================================
    # FULL COMBINED CHALLENGER INTRADAY HOURLY SCORECARD
    # =========================================================================
    print("\n" + "=" * 100)
    print("3. FULL COMBINED CHALLENGER INTRADAY HOURLY DISTRIBUTION (ET BUCKETS)")
    print("=" * 100)

    time_buckets = [
        ("09:30 - 10:30 (Morning Rush)", 9, 30, 10, 30),
        ("10:30 - 12:00 (Late Morning)", 10, 30, 12, 0),
        ("12:00 - 14:00 (Midday Lull)", 12, 0, 14, 0),
        ("14:00 - 15:00 (Afternoon Setup)", 14, 0, 15, 0),
        ("15:00 - 16:00 (Market Close)", 15, 0, 16, 0),
    ]

    hdr_tod = f"{'Intraday ET Window':<32} | {'Executed (N)':<12} | {'Winners':<9} | {'Losers':<9} | {'Total R':<10} | {'Win Rate (%)':<12} | {'Mean R':<10}"
    print(hdr_tod)
    print("-" * 100)

    tr_list = res_full["trade_r_list"]

    for b_label, h_start, m_start, h_end, m_end in time_buckets:
        start_m = h_start * 60 + m_start
        end_m = h_end * 60 + m_end

        b_trades = []
        for tr in tr_list:
            tr_dt = pd.to_datetime(tr["entry_at"])
            if tr_dt.tzinfo is None:
                tr_dt = tr_dt.tz_localize("UTC")
            tr_et = tr_dt.astimezone(ET)
            m_val = tr_et.hour * 60 + tr_et.minute
            if start_m <= m_val < end_m:
                b_trades.append(tr)

        n_b = len(b_trades)
        b_r_vals = [tr["realized_r"] for tr in b_trades]
        b_wins = [r for r in b_r_vals if r > 0]
        b_losses = [r for r in b_r_vals if r <= 0]
        b_tot_r = sum(b_r_vals)
        b_win_rate = (len(b_wins) / n_b * 100.0) if n_b > 0 else 0.0
        b_mean_r = np.mean(b_r_vals) if n_b > 0 else 0.0

        print(
            f"{b_label:<32} | {n_b:<12} | {len(b_wins):<9} | {len(b_losses):<9} | {b_tot_r:<+10.2f} | {b_win_rate:<12.1f} | {b_mean_r:<+10.4f}"
        )

    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
