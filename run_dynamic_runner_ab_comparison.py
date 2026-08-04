"""
Full-Day A/B Comparison: Fixed 2R Take-Profit (Model A) vs. PIS Dynamic Runner Extension (Model B)
==================================================================================================
Date: 2026-08-03
Trades: All 12 completed trades replayed across full market hours (09:30 to 16:00 ET).
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.models import PositionObservation, PISConfig, ExitAction
from app.pis.position_brain import PositionBrain

MASTER_DB = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")

ET = timezone(timedelta(hours=-4))
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)
SENTIMENTS = {'INTC': 0.09, 'CRWV': -0.02, 'NBIS': 0.0, 'NOW': 0.0, 'SPCX': 0.0, 'META': 0.32, 'TQQQ': 0.0, 'AMZN': -0.16, 'BABA': -0.23, 'NVDA': 0.0}

from run_detailed_replay_report import fetch_completed_trades, load_bars, estimate_atr, apply_slippage

def replay_dynamic_pis_runner(trade: dict, bars_conn: sqlite3.Connection) -> dict:
    sym = trade["symbol"]
    side_float = 1.0 if trade["side"] == "long" else -1.0
    qty = float(trade["qty"])
    entry_price = float(trade["entry_price"])
    entry_at = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    entry_epoch = entry_at.timestamp()
    initial_stop = float(trade["stop_loss"])
    target_tp = float(trade["take_profit"]) if trade["take_profit"] is not None else None
    risk_per_share = float(trade["initial_risk"])
    sentiment_score = SENTIMENTS.get(sym, 0.0)

    bars = load_bars(sym, entry_epoch, bars_conn)
    if bars.empty:
        return {"pnl": 0.0, "exit_price": entry_price, "reason": "NO_BARS", "exit_at_et": "N/A", "peak_r": 0.0}

    entry_ts_pd = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]

    row0 = bars_from_entry.iloc[0]
    ts0 = bars_from_entry.index[0].to_pydatetime()
    if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)

    config = PISConfig()
    obs0 = PositionObservation(
        timestamp=ts0, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
        current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
        volume=float(row0["volume"]), atr=1.0, initial_stop=initial_stop, risk_per_share=risk_per_share,
        current_stop=initial_stop, take_profit=None, r_multiple=0.0, news_sentiment_score=sentiment_score
    )

    brain = PositionBrain(sym, obs0, config)
    current_stop = initial_stop
    is_runner = False
    peak_r = 0.0
    highest_price = entry_price if side_float > 0 else entry_price
    exit_price = None
    exit_at = None
    exit_reason = "EOD"

    for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
        bar_ts = bar_ts_pd.to_pydatetime()
        if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)

        idx = bars.index.get_loc(bar_ts_pd)
        atr = estimate_atr(bars, idx)
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        r_mul = (close - entry_price) * side_float / risk_per_share
        
        if side_float > 0 and high > highest_price: highest_price = high
        if side_float < 0 and low < highest_price: highest_price = low
        
        cur_r = (highest_price - entry_price) * side_float / risk_per_share
        if cur_r > peak_r: peak_r = cur_r

        # 1. Stop loss / trailing stop hit check
        if i > 0:
            if side_float == 1.0 and low <= current_stop:
                exit_at = bar_ts
                exit_price = apply_slippage(current_stop, side_float)
                exit_reason = f"STOP_OR_TRAILING_BREACH (${current_stop:.2f})"
                break
            elif side_float == -1.0 and high >= current_stop:
                exit_at = bar_ts
                exit_price = apply_slippage(current_stop, side_float)
                exit_reason = f"STOP_OR_TRAILING_BREACH (${current_stop:.2f})"
                break

        # 2. Market close check
        if bar_ts >= MARKET_CLOSE_UTC:
            exit_price = apply_slippage(close, side_float)
            exit_at = bar_ts
            exit_reason = "MARKET_CLOSE (16:00 ET)"
            break

        obs = PositionObservation(
            timestamp=bar_ts, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
            current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr,
            initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=current_stop,
            take_profit=None, r_multiple=r_mul, news_sentiment_score=sentiment_score
        )

        decision = brain.observe(obs)
        health = decision.health

        # Dynamic Profit Extension Check:
        # If target reached and health is strong (>= 0.70), convert to runner
        target_reached = False
        if target_tp is not None:
            if side_float > 0 and high >= target_tp: target_reached = True
            elif side_float < 0 and low <= target_tp: target_reached = True

        if target_reached and not is_runner:
            if health.overall >= 0.70 or health.momentum_score >= 0.75:
                # Upgrade to Runner! Lock in +0.75R guaranteed profit floor
                is_runner = True
                runner_floor = entry_price + (0.75 * risk_per_share * side_float)
                if side_float > 0:
                    current_stop = max(current_stop, runner_floor)
                else:
                    current_stop = min(current_stop, runner_floor)
            else:
                # Weak health at target -> take standard target exit
                exit_at = bar_ts
                exit_price = apply_slippage(target_tp, side_float)
                exit_reason = f"TARGET_REACHED (${target_tp:.2f}, Health={health.overall:.2f})"
                break

        # If it is a runner, trail stop dynamically behind peak price with 1.5x ATR buffer
        if is_runner:
            if side_float > 0:
                trail_candidate = highest_price - max(1.5 * atr, 0.8 * risk_per_share)
                current_stop = max(current_stop, trail_candidate)
            else:
                trail_candidate = highest_price + max(1.5 * atr, 0.8 * risk_per_share)
                current_stop = min(current_stop, trail_candidate)
        else:
            # Standard PIS protection updates
            if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
                if side_float > 0 and decision.target_stop_loss > current_stop:
                    current_stop = decision.target_stop_loss
                elif side_float < 0 and decision.target_stop_loss < current_stop:
                    current_stop = decision.target_stop_loss

            # Early Distribution Exit (after 3 bars)
            if i >= 3 and decision.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
                reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
                if "DISTRIBUTION_DETECTED" in reasons or "HEALTH_EXHAUSTION" in reasons:
                    exit_at = bar_ts
                    exit_price = apply_slippage(close, side_float)
                    exit_reason = f"EARLY_DISTRIBUTION_EXIT ({','.join(reasons)})"
                    break

    brain.close()

    if exit_price is None:
        close_bars = bars[bars.index <= MARKET_CLOSE_UTC]
        if not close_bars.empty:
            exit_at = close_bars.index[-1].to_pydatetime()
            exit_price = apply_slippage(float(close_bars.iloc[-1]["close"]), side_float)
            exit_reason = "MARKET_CLOSE (16:00 ET)"

    pnl = round((exit_price - entry_price) * side_float * qty, 2)
    return {
        "pnl": pnl,
        "exit_price": round(exit_price, 2),
        "exit_at_et": exit_at.astimezone(ET).strftime("%H:%M:%S") if exit_at else "N/A",
        "reason": exit_reason,
        "peak_r": round(peak_r, 2),
        "is_runner": is_runner,
    }


def main():
    trades = fetch_completed_trades()
    bars_conn = sqlite3.connect(str(MASTER_BARS))

    print("=" * 145)
    print("  FULL-DAY A/B COMPARISON: FIXED 2R BROKER TARGET (MODEL A) vs. PIS DYNAMIC RUNNER EXTENSION (MODEL B)")
    print("=" * 145)
    print(f"{'#':<3} {'SYM':<5} {'STRAT':<14} {'SIDE':<5} | {'MODEL A: FIXED 2R BROKER TP':<32} | {'MODEL B: PIS DYNAMIC RUNNER (5R-6R)':<38} | {'NET DELTA'}")
    print("-" * 145)

    tot_a = 0.0
    tot_b = 0.0
    wins_a = wins_b = 0

    for i, t in enumerate(trades, 1):
        res_b = replay_dynamic_pis_runner(t, bars_conn)
        pnl_a = t["legacy_pnl"]
        pnl_b = res_b["pnl"]
        delta = pnl_b - pnl_a

        tot_a += pnl_a
        tot_b += pnl_b
        if pnl_a > 0: wins_a += 1
        if pnl_b > 0: wins_b += 1

        exit_a_str = f"${t['exit_price']:>7.2f} (${pnl_a:>7.2f}) {t['exit_at'][11:19]}Z"
        exit_b_str = f"${res_b['exit_price']:>7.2f} (${pnl_b:>7.2f}) {res_b['exit_at_et']} [Peak:{res_b['peak_r']}R]"

        print(f"{i:<3} {t['symbol']:<5} {t['strategy']:<14} {t['side'].upper():<5} | {exit_a_str:<32} | {exit_b_str:<38} | {delta:>+8.2f}")

    bars_conn.close()

    print("-" * 145)
    print(f"{'TOTAL REALIZED P&L:':<29} | {'$' + str(round(tot_a, 2)):>32} | {'$' + str(round(tot_b, 2)):>38} | {round(tot_b - tot_a, 2):>+8.2f}")
    print(f"{'WIN RATE:':<29} | {f'{wins_a}/12 ({wins_a/12*100:.1f}%)':>32} | {f'{wins_b}/12 ({wins_b/12*100:.1f}%)':>38} |")
    print("=" * 145)

if __name__ == "__main__":
    main()
