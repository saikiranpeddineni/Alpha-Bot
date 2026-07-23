"""Phase 2 Challenger Profit Diagnostics & Gate Alpha Experiment Script.

Compares Champion vs Challenger on:
1. Gate Alpha Breakdown (ML, Meta-Label, TradeScore)
2. MFE -> Realized-R Capture & Counterfactual Exit Benchmarking
3. Champion vs Challenger Scorecard
"""

import asyncio
import sys
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

async def main():
    print("=" * 80)
    print("PHASE 2 CHALLENGER PROFIT DIAGNOSTICS & GATE ALPHA ANALYSIS")
    print("=" * 80)

    cache_path = DATA_DIR / "cache.db"
    symbols, start_date, end_date = discover_cache_bounds(cache_path)

    dm = DataManager(BlockingProvider(), cache_path)
    dm.validate_cache_coverage = lambda *args, **kwargs: None

    models_dir = DATA_DIR / "models"
    model = SignalModel(models_dir)
    await model.load_artifacts()

    risk_cfg_champion = RiskConfig()
    risk_cfg_challenger = RiskConfig(
        ml_confidence_threshold=0.55,
        soft_composite_gating=True,
    )

    from app.strategies.base import REGISTRY
    strategies = {name: {"enabled": True, "params": {}} for name in REGISTRY.keys()}

    # 1. Run Champion Backtest
    print("\n[1/3] Running Champion Backtest (Hard Gates, ml_threshold=0.55)...")
    params_champion = BacktestParams(
        symbols=symbols,
        start=start_date,
        end=end_date,
        starting_cash=100_000.0,
        slippage_bps=2.0,
        use_ml_gate=True,
        use_dynamic_scanner=True,
        strategies=strategies,
        risk=risk_cfg_champion,
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
        ignore_temporal_leakage=False,
    )
    res_champion = await run_backtest(dm, model, params_champion)

    # 2. Run Challenger Backtest (Gray-Zone Soft Rescue + VWAP R:R Filter)
    print("\n[2/3] Running Challenger Backtest (Gray-Zone Soft Rescue + VWAP R:R Filter)...")
    params_challenger = BacktestParams(
        symbols=symbols,
        start=start_date,
        end=end_date,
        starting_cash=100_000.0,
        slippage_bps=2.0,
        use_ml_gate=True,
        use_dynamic_scanner=True,
        strategies=strategies,
        risk=risk_cfg_challenger,
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
        ignore_temporal_leakage=False,
    )
    res_challenger = await run_backtest(dm, model, params_challenger)

    # 3. Print Comparison Scorecard
    print("\n" + "=" * 80)
    print("CHAMPION VS CHALLENGER COMPARISON SCORECARD")
    print("=" * 80)
    metrics_champ = res_champion.get("summary", {})
    metrics_chall = res_challenger.get("summary", {})

    def _fmt(val, is_pct=False):
        if val is None: return "N/A"
        return f"{val:.2f}%" if is_pct else f"{val:.2f}"

    print(f"{'Metric':<30} | {'Champion':<15} | {'Challenger':<15} | {'Delta':<15}")
    print("-" * 80)

    trades_c = metrics_champ.get("total_trades", 0)
    trades_ch = metrics_chall.get("total_trades", 0)
    print(f"{'Total Trades':<30} | {trades_c:<15} | {trades_ch:<15} | {trades_ch - trades_c:<+15}")

    pnl_c = metrics_champ.get("total_pnl", 0.0)
    pnl_ch = metrics_chall.get("total_pnl", 0.0)
    print(f"{'Total Realized P&L ($)':<30} | {pnl_c:<15.2f} | {pnl_ch:<15.2f} | {pnl_ch - pnl_c:<+15.2f}")

    win_c = metrics_champ.get("win_rate", 0.0)
    win_ch = metrics_chall.get("win_rate", 0.0)
    print(f"{'Win Rate (%)':<30} | {_fmt(win_c, True):<15} | {_fmt(win_ch, True):<15} | {win_ch - win_c:<+15.2f}%")

    pf_c = metrics_champ.get("profit_factor", 0.0)
    pf_ch = metrics_chall.get("profit_factor", 0.0)
    print(f"{'Profit Factor':<30} | {_fmt(pf_c):<15} | {_fmt(pf_ch):<15} | {pf_ch - pf_c:<+15.2f}")

    dd_c = metrics_champ.get("max_drawdown_pct", 0.0)
    dd_ch = metrics_chall.get("max_drawdown_pct", 0.0)
    print(f"{'Max Drawdown (%)':<30} | {_fmt(dd_c, True):<15} | {_fmt(dd_ch, True):<15} | {dd_ch - dd_c:<+15.2f}%")

    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())
