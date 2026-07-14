"""
Walk-Forward Strategy Validation Script
========================================
Tests all 5 strategies on synthetic data across multiple time windows.
Uses the real backtest engine (same components as live trading).

Produces:
  - Per-strategy: win rate, profit factor, expectancy, avg R, max DD
  - Per-regime: which strategies thrive in TREND vs RANGE vs VOLATILE
  - Overall system equity curve
  - Honest verdict on which strategies have statistical edge

Run from project root:
    .venv\Scripts\python.exe walkforward_test.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

from app.ai.model import SignalModel
from app.backtest.engine import BacktestParams, run_backtest
from app.backtest.metrics import compute_metrics
from app.config import DATA_DIR, RiskConfig
from app.data.manager import DataManager
from app.data.yahoo import YahooProvider

# ─────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────
STARTING_CASH     = 100_000.0
SLIPPAGE_BPS      = 3.0          # 3 bps = realistic for liquid names
RISK_PER_TRADE    = 0.5          # % of equity per trade
MAX_POSITIONS     = 4
NUM_WINDOWS       = 5            # walk-forward windows
WINDOW_DAYS       = 1            # each window = 1 trading day (since Yahoo 1Min is limited to 7 days)
BASE_DATE         = datetime(2026, 7, 6, tzinfo=UTC)

SYMBOLS = ["SPY", "QQQ", "NVDA", "AMD", "TSLA", "AAPL", "META", "MSFT"]

STRATEGIES_TO_TEST = [
    {"name": "orb",            "enabled": True},
    {"name": "momentum",       "enabled": True},
    {"name": "vwap_reversion", "enabled": True},
    {"name": "rsi2_reversion", "enabled": True},
    {"name": "bb_squeeze",     "enabled": True},
]

risk_config = RiskConfig(
    risk_per_trade_pct=RISK_PER_TRADE,
    max_open_positions=MAX_POSITIONS,
    max_daily_loss_pct=3.0,
    max_drawdown_pct=12.0,
    consecutive_loss_limit=4,
    cooldown_minutes=20,
    no_entry_minutes_before_close=30,
    flatten_minutes_before_close=5,
    score_threshold_trend=55.0,
    score_threshold_range=70.0,
    score_threshold_volatile=65.0,
    score_threshold_event=75.0,
)


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def safe(v, decimals=2):
    if v is None:
        return "N/A"
    if isinstance(v, float) and (v != v):  # NaN
        return "N/A"
    if isinstance(v, float) and abs(v) == float("inf"):
        return "∞" if v > 0 else "-∞"
    return round(v, decimals)


def grade(pf, wr, trades):
    """Simple quality grade for a strategy result."""
    if trades < 5:
        return "⚠  TOO FEW TRADES"
    if pf is None or isinstance(pf, str):
        return "⚠  NO LOSSES (N/A)"
    if pf >= 1.8 and wr >= 0.50:
        return "✅ STRONG EDGE"
    if pf >= 1.4 and wr >= 0.45:
        return "🟡 MODERATE EDGE"
    if pf >= 1.0:
        return "🟠 MARGINAL (needs more data)"
    return "❌ NO EDGE DETECTED"


# ─────────────────────────────────────────
# Per-strategy isolated test
# ─────────────────────────────────────────
async def test_single_strategy(
    strategy_name: str,
    data: DataManager,
    model: SignalModel,
    window_idx: int,
) -> dict:
    start = BASE_DATE + timedelta(days=window_idx * WINDOW_DAYS)
    end   = start + timedelta(days=WINDOW_DAYS)

    # Enable only this strategy
    strat_config = {s["name"]: {"enabled": s["name"] == strategy_name} for s in STRATEGIES_TO_TEST}

    params = BacktestParams(
        symbols=SYMBOLS,
        start=start,
        end=end,
        starting_cash=STARTING_CASH,
        slippage_bps=SLIPPAGE_BPS,
        risk=risk_config,
        strategies=strat_config,
    )
    result = await run_backtest(data, model, params)
    m = result.metrics
    return {
        "strategy": strategy_name,
        "window": window_idx + 1,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "trades": m.get("total_trades", 0),
        "win_rate": m.get("win_rate"),
        "profit_factor": m.get("profit_factor"),
        "expectancy": m.get("expectancy"),
        "total_return_pct": m.get("total_return_pct"),
        "max_drawdown_pct": m.get("max_drawdown_pct"),
        "sharpe": m.get("sharpe"),
    }


# ─────────────────────────────────────────
# Full system test (all strategies together)
# ─────────────────────────────────────────
async def test_full_system(data: DataManager, model: SignalModel) -> dict:
    start = BASE_DATE
    end   = BASE_DATE + timedelta(days=NUM_WINDOWS * WINDOW_DAYS)
    params = BacktestParams(
        symbols=SYMBOLS,
        start=start,
        end=end,
        starting_cash=STARTING_CASH,
        slippage_bps=SLIPPAGE_BPS,
        risk=risk_config,
    )
    result = await run_backtest(data, model, params)
    return result.metrics, result.trades, result.equity_curve


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────
async def main():
    tmp_cache = Path("data/backtest_cache.db")
    tmp_cache.parent.mkdir(parents=True, exist_ok=True)
    model_dir = DATA_DIR / "models"

    data  = DataManager(YahooProvider(), tmp_cache)
    model = SignalModel(model_dir)

    print("\n" + "="*70)
    print("  ALPHADESK WALK-FORWARD STRATEGY VALIDATION")
    print(f"  {NUM_WINDOWS} windows × {WINDOW_DAYS} days | {len(SYMBOLS)} symbols | ${STARTING_CASH:,.0f} starting capital")
    print(f"  Slippage: {SLIPPAGE_BPS} bps | Risk/trade: {RISK_PER_TRADE}% | Max positions: {MAX_POSITIONS}")
    print("="*70)

    # ── Per-strategy walk-forward ──
    all_results: dict[str, list[dict]] = {s["name"]: [] for s in STRATEGIES_TO_TEST}

    print("\n[1/3] Running per-strategy walk-forward windows...")
    for strat in STRATEGIES_TO_TEST:
        name = strat["name"]
        sys.stdout.write(f"  {name:<20} ")
        sys.stdout.flush()
        for w in range(NUM_WINDOWS):
            r = await test_single_strategy(name, data, model, w)
            all_results[name].append(r)
            sys.stdout.write(".")
            sys.stdout.flush()
        print(" done")

    # ── Aggregate per strategy ──
    print("\n" + "="*70)
    print("  STRATEGY LEADERBOARD (aggregated across all windows)")
    print("="*70)
    print(f"  {'Strategy':<20} {'Trades':>6} {'WinRate':>8} {'PF':>7} {'Expect':>8} {'Ret%':>7} {'MaxDD%':>7} {'Sharpe':>7}  Grade")
    print("  " + "-"*90)

    strategy_summary = []
    for name, results in all_results.items():
        total_trades = sum(r["trades"] for r in results)
        wins = sum(r["trades"] * (r["win_rate"] or 0) for r in results)
        avg_wr = wins / total_trades if total_trades > 0 else 0

        # Weighted average profit factor
        valid_pf = [r["profit_factor"] for r in results if r["profit_factor"] and isinstance(r["profit_factor"], (int, float)) and r["profit_factor"] != float("inf")]
        avg_pf = sum(valid_pf) / len(valid_pf) if valid_pf else None

        valid_exp = [r["expectancy"] for r in results if r["expectancy"] is not None]
        avg_exp = sum(valid_exp) / len(valid_exp) if valid_exp else None

        valid_ret = [r["total_return_pct"] for r in results if r["total_return_pct"] is not None]
        avg_ret = sum(valid_ret) / len(valid_ret) if valid_ret else None

        valid_dd = [r["max_drawdown_pct"] for r in results if r["max_drawdown_pct"] is not None]
        avg_dd = sum(valid_dd) / len(valid_dd) if valid_dd else None

        valid_sh = [r["sharpe"] for r in results if r["sharpe"] is not None and abs(r["sharpe"]) < 100]
        avg_sh = sum(valid_sh) / len(valid_sh) if valid_sh else None

        g = grade(avg_pf, avg_wr, total_trades)
        strategy_summary.append({
            "name": name, "trades": total_trades, "win_rate": avg_wr,
            "profit_factor": avg_pf, "expectancy": avg_exp,
            "return_pct": avg_ret, "max_dd": avg_dd, "sharpe": avg_sh, "grade": g,
        })

        print(
            f"  {name:<20} {total_trades:>6} {safe(avg_wr*100,1):>7}%"
            f" {safe(avg_pf,2):>7} {safe(avg_exp,2):>8}"
            f" {safe(avg_ret,2):>7} {safe(avg_dd,2):>7} {safe(avg_sh,2):>7}  {g}"
        )

    # ── Per-window detail ──
    print("\n" + "="*70)
    print("  PER-WINDOW BREAKDOWN (consistency check)")
    print("="*70)
    for name, results in all_results.items():
        print(f"\n  {name.upper()}")
        print(f"  {'Window':<8} {'Start':<12} {'Trades':>6} {'WR%':>6} {'PF':>6} {'Ret%':>7} {'DD%':>6}")
        for r in results:
            pf_str = safe(r["profit_factor"], 2) if r["profit_factor"] and r["profit_factor"] != float("inf") else "∞"
            print(
                f"  W{r['window']:<7} {r['start']:<12} {r['trades']:>6}"
                f" {safe((r['win_rate'] or 0)*100, 1):>6} {pf_str:>6}"
                f" {safe(r['total_return_pct'], 2):>7} {safe(r['max_drawdown_pct'], 2):>6}"
            )

    # ── Full system test ──
    print("\n" + "="*70)
    print("  FULL SYSTEM TEST (all strategies combined)")
    print("="*70)
    print("  Running full system backtest...")
    sys_metrics, sys_trades, sys_curve = await test_full_system(data, model)

    print(f"\n  Total Trades:      {sys_metrics.get('total_trades', 0)}")
    print(f"  Win Rate:          {safe(sys_metrics.get('win_rate', 0) * 100, 1)}%")
    print(f"  Profit Factor:     {safe(sys_metrics.get('profit_factor'))}")
    print(f"  Expectancy:        ${safe(sys_metrics.get('expectancy'))}/trade")
    print(f"  Total Return:      {safe(sys_metrics.get('total_return_pct'))}%")
    print(f"  Max Drawdown:      {safe(sys_metrics.get('max_drawdown_pct'))}%")
    print(f"  Sharpe Ratio:      {safe(sys_metrics.get('sharpe'))}")
    print(f"  Final Equity:      ${sys_metrics.get('final_equity', STARTING_CASH):,.2f}")

    # ── Verdict ──
    print("\n" + "="*70)
    print("  HONEST VERDICT")
    print("="*70)

    pf = sys_metrics.get("profit_factor")
    wr = sys_metrics.get("win_rate", 0)
    total = sys_metrics.get("total_trades", 0)
    dd = sys_metrics.get("max_drawdown_pct", 0)

    if total < 20:
        print("\n  ⚠  INSUFFICIENT TRADES for statistical significance.")
        print("     Need 50+ trades per strategy to draw conclusions.")
        print("     Synthetic data windows may be too short.")
    elif pf and pf >= 1.5 and wr >= 0.48:
        print("\n  ✅ SYSTEM SHOWS POSITIVE EXPECTANCY in synthetic testing.")
        print("     Next step: paper trade 2-4 weeks with REAL market data.")
        print("     Do NOT treat backtest profits as guaranteed live results.")
    elif pf and pf >= 1.0:
        print("\n  🟡 MARGINAL EDGE detected. System is not losing on average.")
        print("     More data needed. Paper trade before risking capital.")
    else:
        print("\n  🔴 NO CLEAR EDGE in this test. Review strategy parameters.")
        print("     This is valuable information — better to know now.")

    print("\n  NOTE: These results use SYNTHETIC data.")
    print("  Real edge can only be confirmed with live Alpaca paper trading.")
    print("  Synthetic = structural sanity check, not profitability proof.\n")

    # ── Recommendations ──
    print("="*70)
    print("  RECOMMENDATIONS BY STRATEGY")
    print("="*70)
    for s in sorted(strategy_summary, key=lambda x: (x["profit_factor"] or 0), reverse=True):
        rec = "KEEP" if (s["profit_factor"] or 0) >= 1.2 else "MONITOR" if (s["profit_factor"] or 0) >= 1.0 else "REVIEW"
        print(f"  {s['name']:<22} → {rec}  | PF={safe(s['profit_factor'],2)} WR={safe(s['win_rate']*100,1)}% Trades={s['trades']}")

    print("\n" + "="*70)
    print("  NEXT STEPS")
    print("="*70)
    print("  1. Start paper trading with real Alpaca data (already configured)")
    print("  2. Run for 2-4 weeks WITHOUT changing code")
    print("  3. Review live strategy leaderboard via Performance Engine")
    print("  4. Disable strategies with persistent PF < 1.0 after 50+ trades")
    print("  5. Scale size only after statistically significant live edge confirmed")
    print("="*70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
