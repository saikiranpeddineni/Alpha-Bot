"""
Three-Way A/B/C Comprehensive Comparison
=========================================
Model A: Legacy Master Bot (Broker Brackets + Baseline PIS)
Model B: Current Bot_QA PIS (Discretionary Point-in-Time Health)
Model C: Optimized PIS 2.0 (3-Bar Incubation Window + EMA Health Smoothing)
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

from app.pis.models import PositionObservation, PISConfig, ExitAction, LifecycleState
from app.pis.position_brain import PositionBrain

MASTER_DB = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")

SLIPPAGE_BPS = 2
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)
ET = timezone(timedelta(hours=-4))
SENTIMENTS = {'INTC': 0.09, 'CRWV': -0.02, 'NBIS': 0.0, 'NOW': 0.0, 'SPCX': 0.0, 'META': 0.32, 'TQQQ': 0.0, 'AMZN': -0.16, 'BABA': -0.23, 'NVDA': 0.0}

from run_detailed_replay_report import fetch_completed_trades, load_bars, estimate_atr, apply_slippage


def replay_model_b(trade: dict, bars_conn: sqlite3.Connection) -> dict:
    """Current Bot_QA PIS (Model B)."""
    config = PISConfig()
    symbol = trade["symbol"]
    side_float = 1.0 if trade["side"] == "long" else -1.0
    qty = float(trade["qty"])
    entry_price = float(trade["entry_price"])
    entry_at = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    entry_epoch = entry_at.timestamp()
    initial_stop = float(trade["stop_loss"])
    take_profit = float(trade["take_profit"]) if trade["take_profit"] is not None else None
    risk_per_share = float(trade["initial_risk"])
    sentiment_score = SENTIMENTS.get(symbol, 0.0)

    bars = load_bars(symbol, entry_epoch, bars_conn)
    if bars.empty:
        return {"pnl": 0.0, "exit_price": entry_price, "reason": "NO_BARS", "bars": 0}

    entry_ts_pd = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]

    row0 = bars_from_entry.iloc[0]
    ts0 = bars_from_entry.index[0].to_pydatetime()
    if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)

    obs0 = PositionObservation(
        timestamp=ts0, symbol=symbol, side=side_float, qty=qty, entry_price=entry_price,
        current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
        volume=float(row0["volume"]), atr=1.0, initial_stop=initial_stop, risk_per_share=risk_per_share,
        current_stop=initial_stop, take_profit=take_profit, r_multiple=0.0, news_sentiment_score=sentiment_score
    )

    brain = PositionBrain(symbol, obs0, config)
    current_stop = initial_stop
    new_exit_price = None
    new_exit_at = None
    new_exit_reason = "EOD"
    bars_held = 0

    for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
        bar_ts = bar_ts_pd.to_pydatetime()
        if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)

        idx = bars.index.get_loc(bar_ts_pd)
        atr = estimate_atr(bars, idx)
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        vol = float(row["volume"])
        r_mul = (close - entry_price) * side_float / risk_per_share

        if i > 0:
            if side_float == 1.0 and low <= current_stop:
                new_exit_at = bar_ts
                new_exit_price = apply_slippage(current_stop, side_float)
                new_exit_reason = f"ACTIVE_STOP_BREACH (${current_stop:.2f})"
                bars_held = i + 1
                break
            elif side_float == -1.0 and high >= current_stop:
                new_exit_at = bar_ts
                new_exit_price = apply_slippage(current_stop, side_float)
                new_exit_reason = f"ACTIVE_STOP_BREACH (${current_stop:.2f})"
                bars_held = i + 1
                break

        if bar_ts >= MARKET_CLOSE_UTC:
            new_exit_price = apply_slippage(close, side_float)
            new_exit_at = bar_ts
            new_exit_reason = "MARKET_CLOSE"
            bars_held = i + 1
            break

        obs = PositionObservation(
            timestamp=bar_ts, symbol=symbol, side=side_float, qty=qty, entry_price=entry_price,
            current_price=close, high=high, low=low, close=close, volume=vol, atr=atr,
            initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=current_stop,
            take_profit=take_profit, r_multiple=r_mul, news_sentiment_score=sentiment_score
        )

        decision = brain.observe(obs)
        reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
        reason_str = ",".join(reasons)

        if decision.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
            new_exit_at = bar_ts
            if "PROTECTION_BREACH" in reason_str:
                new_exit_price = apply_slippage(current_stop if (side_float > 0 and low <= current_stop) or (side_float < 0 and high >= current_stop) else close, side_float)
            elif "HEALTHY_TREND" in reason_str and take_profit and ((side_float > 0 and high >= take_profit) or (side_float < 0 and low <= take_profit)):
                new_exit_price = apply_slippage(take_profit, side_float)
            else:
                new_exit_price = apply_slippage(close, side_float)
            new_exit_reason = f"PIS Exit ({reason_str})"
            bars_held = i + 1
            break

        if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
            current_stop = decision.target_stop_loss

    brain.close()

    if new_exit_price is None:
        close_bars = bars[bars.index <= MARKET_CLOSE_UTC]
        if not close_bars.empty:
            new_exit_at = close_bars.index[-1].to_pydatetime()
            new_exit_price = apply_slippage(float(close_bars.iloc[-1]["close"]), side_float)
            new_exit_reason = "MARKET_CLOSE"
            bars_held = len(bars_from_entry)

    pnl = round((new_exit_price - entry_price) * side_float * qty, 2)
    return {
        "pnl": pnl,
        "exit_price": new_exit_price,
        "exit_at_et": new_exit_at.astimezone(ET).strftime("%H:%M:%S") if new_exit_at else "N/A",
        "reason": new_exit_reason,
        "bars": bars_held
    }


def replay_model_c(trade: dict, bars_conn: sqlite3.Connection) -> dict:
    """Optimized PIS 2.0 (Model C): 3-Bar Incubation + EMA Health Smoothing."""
    config = PISConfig()
    symbol = trade["symbol"]
    side_float = 1.0 if trade["side"] == "long" else -1.0
    qty = float(trade["qty"])
    entry_price = float(trade["entry_price"])
    entry_at = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    entry_epoch = entry_at.timestamp()
    initial_stop = float(trade["stop_loss"])
    take_profit = float(trade["take_profit"]) if trade["take_profit"] is not None else None
    risk_per_share = float(trade["initial_risk"])
    sentiment_score = SENTIMENTS.get(symbol, 0.0)

    bars = load_bars(symbol, entry_epoch, bars_conn)
    if bars.empty:
        return {"pnl": 0.0, "exit_price": entry_price, "reason": "NO_BARS", "bars": 0}

    entry_ts_pd = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]

    row0 = bars_from_entry.iloc[0]
    ts0 = bars_from_entry.index[0].to_pydatetime()
    if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)

    obs0 = PositionObservation(
        timestamp=ts0, symbol=symbol, side=side_float, qty=qty, entry_price=entry_price,
        current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
        volume=float(row0["volume"]), atr=1.0, initial_stop=initial_stop, risk_per_share=risk_per_share,
        current_stop=initial_stop, take_profit=take_profit, r_multiple=0.0, news_sentiment_score=sentiment_score
    )

    brain = PositionBrain(symbol, obs0, config)
    current_stop = initial_stop
    new_exit_price = None
    new_exit_at = None
    new_exit_reason = "EOD"
    bars_held = 0
    
    smoothed_health = 0.50

    for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
        bar_ts = bar_ts_pd.to_pydatetime()
        if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)

        idx = bars.index.get_loc(bar_ts_pd)
        atr = estimate_atr(bars, idx)
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        vol = float(row["volume"])
        r_mul = (close - entry_price) * side_float / risk_per_share

        # 1. Hard Stop breach check
        if i > 0:
            if side_float == 1.0 and low <= current_stop:
                new_exit_at = bar_ts
                new_exit_price = apply_slippage(current_stop, side_float)
                new_exit_reason = f"ACTIVE_STOP_BREACH (${current_stop:.2f})"
                bars_held = i + 1
                break
            elif side_float == -1.0 and high >= current_stop:
                new_exit_at = bar_ts
                new_exit_price = apply_slippage(current_stop, side_float)
                new_exit_reason = f"ACTIVE_STOP_BREACH (${current_stop:.2f})"
                bars_held = i + 1
                break

        if bar_ts >= MARKET_CLOSE_UTC:
            new_exit_price = apply_slippage(close, side_float)
            new_exit_at = bar_ts
            new_exit_reason = "MARKET_CLOSE"
            bars_held = i + 1
            break

        obs = PositionObservation(
            timestamp=bar_ts, symbol=symbol, side=side_float, qty=qty, entry_price=entry_price,
            current_price=close, high=high, low=low, close=close, volume=vol, atr=atr,
            initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=current_stop,
            take_profit=take_profit, r_multiple=r_mul, news_sentiment_score=sentiment_score
        )

        decision = brain.observe(obs)
        raw_health = decision.health.overall
        smoothed_health = 0.5 * raw_health + 0.5 * smoothed_health
        
        reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
        reason_str = ",".join(reasons)

        # 3-Bar Incubation Filter:
        # In bars 1 to 3, suppress pure early distribution exits UNLESS hard stop breached or R < -0.75R
        is_incubating = (i < 3)
        should_exit = False
        
        if decision.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
            if "PROTECTION_BREACH" in reason_str or (take_profit and ((side_float > 0 and high >= take_profit) or (side_float < 0 and low <= take_profit))):
                should_exit = True
            elif not is_incubating and smoothed_health < config.health_distribution_threshold:
                should_exit = True
            elif is_incubating and r_mul < -0.75:
                should_exit = True
            else:
                # Incubation holding
                should_exit = False

        if should_exit:
            new_exit_at = bar_ts
            if "PROTECTION_BREACH" in reason_str:
                new_exit_price = apply_slippage(current_stop if (side_float > 0 and low <= current_stop) or (side_float < 0 and high >= current_stop) else close, side_float)
            elif "HEALTHY_TREND" in reason_str and take_profit and ((side_float > 0 and high >= take_profit) or (side_float < 0 and low <= take_profit)):
                new_exit_price = apply_slippage(take_profit, side_float)
            else:
                new_exit_price = apply_slippage(close, side_float)
            new_exit_reason = f"PIS 2.0 Exit ({reason_str})"
            bars_held = i + 1
            break

        if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
            current_stop = decision.target_stop_loss

    brain.close()

    if new_exit_price is None:
        close_bars = bars[bars.index <= MARKET_CLOSE_UTC]
        if not close_bars.empty:
            new_exit_at = close_bars.index[-1].to_pydatetime()
            new_exit_price = apply_slippage(float(close_bars.iloc[-1]["close"]), side_float)
            new_exit_reason = "MARKET_CLOSE"
            bars_held = len(bars_from_entry)

    pnl = round((new_exit_price - entry_price) * side_float * qty, 2)
    return {
        "pnl": pnl,
        "exit_price": new_exit_price,
        "exit_at_et": new_exit_at.astimezone(ET).strftime("%H:%M:%S") if new_exit_at else "N/A",
        "reason": new_exit_reason,
        "bars": bars_held
    }


def main():
    trades = fetch_completed_trades()
    bars_conn = sqlite3.connect(str(MASTER_BARS))

    results = []
    for t in trades:
        res_b = replay_model_b(t, bars_conn)
        res_c = replay_model_c(t, bars_conn)
        results.append({
            "trade": t,
            "model_a": {
                "pnl": t["legacy_pnl"],
                "exit_price": t["exit_price"],
                "exit_at_et": datetime.fromisoformat(t["exit_at"].replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M:%S"),
                "reason": t["legacy_reason"],
            },
            "model_b": res_b,
            "model_c": res_c,
        })
    bars_conn.close()

    print("=" * 140)
    print("  THREE-WAY A / B / C REPLAY PERFORMANCE COMPARISON (2026-08-03)")
    print("=" * 140)
    print(f"{'#':<3} {'SYM':<5} {'STRAT':<14} {'SIDE':<5} | {'MODEL A (MASTER)':<24} | {'MODEL B (CUR PIS)':<24} | {'MODEL C (PIS 2.0 OPT)':<24} | {'C vs A DELTA'}")
    print("-" * 140)

    tot_a = 0.0
    tot_b = 0.0
    tot_c = 0.0
    wins_a = wins_b = wins_c = 0

    for i, r in enumerate(results, 1):
        t = r["trade"]
        a = r["model_a"]
        b = r["model_b"]
        c = r["model_c"]

        tot_a += a["pnl"]
        tot_b += b["pnl"]
        tot_c += c["pnl"]

        if a["pnl"] > 0: wins_a += 1
        if b["pnl"] > 0: wins_b += 1
        if c["pnl"] > 0: wins_c += 1

        delta_ca = c["pnl"] - a["pnl"]
        delta_cb = c["pnl"] - b["pnl"]

        str_a = f"${a['exit_price']:>7.2f} (${a['pnl']:>7.2f}) {a['exit_at_et']}"
        str_b = f"${b['exit_price']:>7.2f} (${b['pnl']:>7.2f}) {b['exit_at_et']}"
        str_c = f"${c['exit_price']:>7.2f} (${c['pnl']:>7.2f}) {c['exit_at_et']}"

        print(f"{i:<3} {t['symbol']:<5} {t['strategy']:<14} {t['side'].upper():<5} | {str_a:<24} | {str_b:<24} | {str_c:<24} | {delta_ca:>+8.2f}")

    print("-" * 140)
    print(f"{'TOTAL REALIZED P&L:':<29} | {'$' + str(round(tot_a, 2)):>24} | {'$' + str(round(tot_b, 2)):>24} | {'$' + str(round(tot_c, 2)):>24} | {round(tot_c - tot_a, 2):>+8.2f}")
    print(f"{'WIN RATE:':<29} | {f'{wins_a}/12 ({wins_a/12*100:.1f}%)':>24} | {f'{wins_b}/12 ({wins_b/12*100:.1f}%)':>24} | {f'{wins_c}/12 ({wins_c/12*100:.1f}%)':>24} |")
    print("=" * 140)

if __name__ == "__main__":
    main()
