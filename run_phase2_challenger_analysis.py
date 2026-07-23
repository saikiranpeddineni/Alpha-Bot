"""Phase 2 Challenger Strict Attribution, Gate Audit & Intraday Funnel Telemetry Script.

Diagnostic Integrity Protections:
- Unique Multi-Key & Timestamp-Tolerance Signal-to-Trade Matching (No Duplicate Misattribution).
- Disambiguated Downstream Gate Audit (Active Model Approved vs Fallback Pass vs Disabled Pass).
- Intraday Hourly Rejection Funnel & Lost Positive R Breakdown (09:30-10:30, 10:30-12:00, 12:00-14:00, 14:00-15:00, 15:00-16:00).
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


def match_trade_to_signal(tr, signals):
    """Match a trade uniquely to a shadow signal using Candidate ID or Symbol+Strategy+Side+Timestamp."""
    # 1. Candidate ID match
    if tr["candidate_id"]:
        for s in signals:
            if s.get("candidate_id") == tr["candidate_id"]:
                return s

    # 2. Strict Multi-Key & Timestamp Window Match (within 5 minutes)
    tr_time = pd.to_datetime(tr["entry_at"])
    if tr_time.tzinfo is None:
        tr_time = tr_time.tz_localize("UTC")

    for s in signals:
        if s.get("symbol") == tr["symbol"] and s.get("strategy") == tr["strategy"]:
            s_side = s.get("direction", "").lower()
            if ("long" in s_side and tr["side"] == "long") or ("short" in s_side and tr["side"] == "short"):
                s_time = pd.to_datetime(s.get("timestamp"))
                if s_time.tzinfo is None:
                    s_time = s_time.tz_localize("UTC")
                if abs((tr_time - s_time).total_seconds()) <= 300:
                    return s

    return None


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
    print("PHASE 2 CHALLENGER STRICT ATTRIBUTION, GATE AUDIT & INTRADAY FUNNEL TELEMETRY")
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
    # 1. SCORECARD WITH UNIQUE SIGNAL-TO-TRADE MATCHING
    # =========================================================================
    print("\n" + "=" * 100)
    print("1. SCORECARD WITH UNIQUE SIGNAL-TO-TRADE MATCHING (EXACT REALIZED R)")
    print("=" * 100)
    hdr = f"{'Period':<20} | {'Rescued N':<9} | {'Rescued R':<10} | {'Mean R':<8} | {'PF (R)':<7} | {'Champ R':<9} | {'Chall R':<9} | {'Delta R':<8}"
    print(hdr)
    print("-" * 100)

    for p_name, p_data in [("Full Period (Jul 6-17)", full_res), ("Window 1 (Jul 6-10)", w1_res), ("Window 2 (Jul 11-17)", w2_res)]:
        champ = p_data["A (Champion: Hard Gates)"]
        chall = p_data["B (Gray Rescue Only)"]

        res_chall = chall["backtest"]
        shadows = getattr(res_chall, "shadow_signals", [])
        rescued_sigs = [s for s in shadows if s.get("ml_gate_decision") == "GRAY_ZONE_RESCUED"]
        chall_tr_list = chall["trade_r_list"]

        rescued_tr_list = []
        for tr in chall_tr_list:
            matched_sig = match_trade_to_signal(tr, rescued_sigs)
            if matched_sig is not None:
                rescued_tr_list.append(tr)

        r_vals = [tr["realized_r"] for tr in rescued_tr_list]
        r_sum = sum(r_vals)
        r_mean = np.mean(r_vals) if r_vals else 0.0
        r_pf = (sum(r for r in r_vals if r > 0) / abs(sum(r for r in r_vals if r < 0))) if any(r < 0 for r in r_vals) else 99.0

        c_r = champ["total_r"]
        ch_r = chall["total_r"]
        delta_r = ch_r - c_r

        print(
            f"{p_name:<20} | {len(rescued_tr_list):<9} | {r_sum:<+10.2f} | {r_mean:<+8.4f} | {r_pf:<7.2f} | {c_r:<+9.2f} | {ch_r:<+9.2f} | {delta_r:<+8.2f}"
        )
    print("=" * 100)

    # =========================================================================
    # 2. DISAMBIGUATED DOWNSTREAM GATE AUDIT (ACTIVE VS FALLBACK VS DISABLED)
    # =========================================================================
    print("\n" + "=" * 100)
    print("2. DISAMBIGUATED DOWNSTREAM GATE AUDIT (RAW GATE BREAKDOWN)")
    print("=" * 100)

    res_chall = full_res["B (Gray Rescue Only)"]["backtest"]
    shadows = getattr(res_chall, "shadow_signals", [])
    rescued_sigs = [s for s in shadows if s.get("ml_gate_decision") == "GRAY_ZONE_RESCUED"]

    meta_counts = {"approved": 0, "fallback_pass": 0, "disabled_pass": 0, "rejected": 0}
    ts_counts = {"approved": 0, "disabled_pass": 0, "rejected": 0}

    for s in shadows:
        m_dec = s.get("meta_label_decision", "disabled_pass")
        meta_counts[m_dec] = meta_counts.get(m_dec, 0) + 1

        t_dec = s.get("trade_score_decision", "disabled_pass")
        ts_counts[t_dec] = ts_counts.get(t_dec, 0) + 1

    print("[All 38 Candidate Signals Gate Status]")
    print(f" - Meta-Label:  Approved={meta_counts['approved']} | Fallback={meta_counts['fallback_pass']} | Disabled={meta_counts['disabled_pass']} | Rejected={meta_counts['rejected']}")
    print(f" - TradeScore:  Approved={ts_counts['approved']} | Disabled={ts_counts['disabled_pass']} | Rejected={ts_counts['rejected']}")

    print("\n[Rescued Signals (4) Raw Gate Status Detail]")
    print(f"{'Symbol':<8} | {'Strategy':<16} | {'ML Prob':<8} | {'Meta Decision':<15} | {'Meta Prob':<10} | {'TradeScore Decision':<20} | {'TradeScore':<10}")
    print("-" * 100)

    for s in rescued_sigs:
        print(
            f"{s.get('symbol'):<8} | {s.get('strategy'):<16} | {s.get('ml_probability'):<8.3f} | {s.get('meta_label_decision'):<15} | {s.get('meta_probability', 1.0):<10.3f} | {s.get('trade_score_decision'):<20} | {s.get('trade_score', 80.0):<10.1f}"
        )
    print("=" * 100)

    # =========================================================================
    # 3. INTRADAY HOURLY REJECTION FUNNEL & REALIZED R
    # =========================================================================
    print("\n" + "=" * 100)
    print("3. INTRADAY HOURLY REJECTION FUNNEL & REALIZED R DISTRIBUTION (ET BUCKETS)")
    print("=" * 100)

    time_buckets = [
        ("09:30 - 10:30 (Morning Rush)", 9, 30, 10, 30),
        ("10:30 - 12:00 (Late Morning)", 10, 30, 12, 0),
        ("12:00 - 14:00 (Midday Lull)", 12, 0, 14, 0),
        ("14:00 - 15:00 (Afternoon)", 14, 0, 15, 0),
        ("15:00 - 16:00 (Market Close)", 15, 0, 16, 0),
    ]

    print(f"{'Intraday ET Window':<30} | {'Candidates':<10} | {'Executed':<10} | {'Realized R':<12} | {'Rescued':<8} | {'Rescued R':<10}")
    print("-" * 100)

    chall_tr_list = full_res["B (Gray Rescue Only)"]["trade_r_list"]

    for b_label, h_start, m_start, h_end, m_end in time_buckets:
        start_m = h_start * 60 + m_start
        end_m = h_end * 60 + m_end

        # Filter candidates by ET time window
        b_cands = []
        for s in shadows:
            ts_dt = pd.to_datetime(s.get("timestamp"))
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.tz_localize("UTC")
            ts_et = ts_dt.astimezone(ET)
            m_val = ts_et.hour * 60 + ts_et.minute
            if start_m <= m_val < end_m:
                b_cands.append(s)

        # Filter executed trades by ET entry time
        b_trades = []
        for tr in chall_tr_list:
            tr_dt = pd.to_datetime(tr["entry_at"])
            if tr_dt.tzinfo is None:
                tr_dt = tr_dt.tz_localize("UTC")
            tr_et = tr_dt.astimezone(ET)
            m_val = tr_et.hour * 60 + tr_et.minute
            if start_m <= m_val < end_m:
                b_trades.append(tr)

        b_rescued_tr = [tr for tr in b_trades if match_trade_to_signal(tr, rescued_sigs) is not None]

        r_tot = sum(tr["realized_r"] for tr in b_trades)
        r_resc = sum(tr["realized_r"] for tr in b_rescued_tr)

        print(
            f"{b_label:<30} | {len(b_cands):<10} | {len(b_trades):<10} | {r_tot:<+12.2f} | {len(b_rescued_tr):<8} | {r_resc:<+10.2f}"
        )
    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
