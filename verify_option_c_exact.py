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

MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
ET = timezone(timedelta(hours=-4))
SENTIMENTS = {'INTC': 0.09, 'CRWV': -0.02, 'NBIS': 0.0, 'NOW': 0.0, 'SPCX': 0.0, 'META': 0.32, 'TQQQ': 0.0, 'AMZN': -0.16, 'BABA': -0.23, 'NVDA': 0.0}

from run_detailed_replay_report import fetch_completed_trades, load_bars, estimate_atr

trades = fetch_completed_trades()
bars_conn = sqlite3.connect(str(MASTER_BARS))

print("=" * 145)
print(f"{'#':<3} {'SYM':<5} {'STRATEGY':<14} {'SIDE':<5} | {'MODEL A (MASTER)':<18} | {'MODEL B (CURRENT PIS)':<22} | {'MODEL C (CALIBRATED PIS 2.0)':<28} | {'DELTA (C vs A)'}")
print("=" * 145)

tot_a = tot_b = tot_c = 0.0
wins_a = wins_b = wins_c = 0

for idx, trade in enumerate(trades, 1):
    sym = trade["symbol"]
    side_str = trade["side"].upper()
    side_float = 1.0 if trade["side"] == "long" else -1.0
    qty = float(trade["qty"])
    entry_price = float(trade["entry_price"])
    entry_at = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    initial_stop = float(trade["stop_loss"])
    target_tp = float(trade["take_profit"]) if trade["take_profit"] is not None else None
    risk_per_share = float(trade["initial_risk"])
    sentiment_score = SENTIMENTS.get(sym, 0.0)

    bars = load_bars(sym, entry_at.timestamp(), bars_conn)
    entry_ts_pd = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]

    # --- SIMULATE MODEL B (CURRENT PIS) ---
    config_b = PISConfig()
    row0 = bars_from_entry.iloc[0]
    ts0 = bars_from_entry.index[0].to_pydatetime()
    if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)

    obs0 = PositionObservation(
        timestamp=ts0, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
        current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
        volume=float(row0["volume"]), atr=1.0, initial_stop=initial_stop, risk_per_share=risk_per_share,
        current_stop=initial_stop, take_profit=target_tp, r_multiple=0.0, news_sentiment_score=sentiment_score
    )

    brain_b = PositionBrain(sym, obs0, config_b)
    stop_b = initial_stop
    exit_px_b = None
    reason_b = ""

    for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
        bar_ts = bar_ts_pd.to_pydatetime()
        if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
        idx_b = bars.index.get_loc(bar_ts_pd)
        atr_b = estimate_atr(bars, idx_b)
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        r_mul = (close - entry_price) * side_float / risk_per_share

        if i > 0:
            if side_float == 1.0 and low <= stop_b:
                exit_px_b = stop_b
                reason_b = "STOP_BREACH"
                break
            elif side_float == -1.0 and high >= stop_b:
                exit_px_b = stop_b
                reason_b = "STOP_BREACH"
                break

        obs_b = PositionObservation(
            timestamp=bar_ts, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
            current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr_b,
            initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=stop_b,
            take_profit=target_tp, r_multiple=r_mul, news_sentiment_score=sentiment_score
        )
        dec_b = brain_b.observe(obs_b)
        if dec_b.target_stop_loss is not None and dec_b.target_stop_loss > 0:
            if side_float > 0 and dec_b.target_stop_loss > stop_b: stop_b = dec_b.target_stop_loss
            elif side_float < 0 and dec_b.target_stop_loss < stop_b: stop_b = dec_b.target_stop_loss

        if dec_b.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
            reasons = [r.value if hasattr(r, "value") else str(r) for r in dec_b.reason_codes]
            exit_px_b = close if "PROTECTION_BREACH" not in reasons else stop_b
            reason_b = f"{dec_b.action.value} ({','.join(reasons)})"
            break
    brain_b.close()
    if exit_px_b is None: exit_px_b = float(bars_from_entry.iloc[-1]["close"])
    pnl_b = round((exit_px_b - entry_price) * side_float * qty, 2)

    # --- SIMULATE MODEL C (CALIBRATED PIS 2.0) ---
    # Model C uses: 2-bar distribution confirmation window + Passive Limit Fill at Target + EMA Health
    brain_c = PositionBrain(sym, obs0, config_b)
    stop_c = initial_stop
    exit_px_c = None
    reason_c = ""
    dist_count = 0

    for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
        bar_ts = bar_ts_pd.to_pydatetime()
        if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
        idx_c = bars.index.get_loc(bar_ts_pd)
        atr_c = estimate_atr(bars, idx_c)
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        r_mul = (close - entry_price) * side_float / risk_per_share

        # Target passive limit fill check
        if target_tp is not None:
            if side_float == 1.0 and high >= target_tp:
                exit_px_c = target_tp
                reason_c = "TARGET_LIMIT_FILL"
                break
            elif side_float == -1.0 and low <= target_tp:
                exit_px_c = target_tp
                reason_c = "TARGET_LIMIT_FILL"
                break

        if i > 0:
            if side_float == 1.0 and low <= stop_c:
                exit_px_c = stop_c
                reason_c = "STOP_BREACH"
                break
            elif side_float == -1.0 and high >= stop_c:
                exit_px_c = stop_c
                reason_c = "STOP_BREACH"
                break

        obs_c = PositionObservation(
            timestamp=bar_ts, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
            current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr_c,
            initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=stop_c,
            take_profit=target_tp, r_multiple=r_mul, news_sentiment_score=sentiment_score
        )
        dec_c = brain_c.observe(obs_c)
        if dec_c.target_stop_loss is not None and dec_c.target_stop_loss > 0:
            if side_float > 0 and dec_c.target_stop_loss > stop_c: stop_c = dec_c.target_stop_loss
            elif side_float < 0 and dec_c.target_stop_loss < stop_c: stop_c = dec_c.target_stop_loss

        reasons_c = [r.value if hasattr(r, "value") else str(r) for r in dec_c.reason_codes]
        if "DISTRIBUTION_DETECTED" in reasons_c or "HEALTH_EXHAUSTION" in reasons_c:
            dist_count += 1
            # Requires 2 consecutive bars of distribution OR loss > -0.5R to cut immediately
            if dist_count >= 2 or r_mul <= -0.50:
                exit_px_c = close
                reason_c = f"CONFIRMED_DISTRIBUTION ({','.join(reasons_c)})"
                break
        else:
            dist_count = 0

        if "PROTECTION_BREACH" in reasons_c:
            exit_px_c = stop_c
            reason_c = "PROTECTION_BREACH"
            break
    brain_c.close()
    if exit_px_c is None: exit_px_c = float(bars_from_entry.iloc[-1]["close"])
    pnl_c = round((exit_px_c - entry_price) * side_float * qty, 2)

    pnl_a = trade["legacy_pnl"]
    tot_a += pnl_a
    tot_b += pnl_b
    tot_c += pnl_c
    if pnl_a > 0: wins_a += 1
    if pnl_b > 0: wins_b += 1
    if pnl_c > 0: wins_c += 1

    delta_c_a = pnl_c - pnl_a
    print(f"{idx:<3} {sym:<5} {trade['strategy']:<14} {side_str:<5} | ${pnl_a:>8.2f} ({trade['exit_price']:.2f}) | ${pnl_b:>8.2f} ({exit_px_b:.2f})    | ${pnl_c:>8.2f} ({exit_px_c:.2f})       | {delta_c_a:>+8.2f}")

bars_conn.close()

print("=" * 145)
print(f"{'TOTAL REALIZED P&L:':<30} | ${tot_a:>16.2f} | ${tot_b:>20.2f} | ${tot_c:>26.2f} | ${tot_c - tot_a:>+8.2f}")
print(f"{'WIN RATE:':<30} | {f'{wins_a}/12 ({wins_a/12*100:.1f}%)':>18} | {f'{wins_b}/12 ({wins_b/12*100:.1f}%)':>22} | {f'{wins_c}/12 ({wins_c/12*100:.1f}%)':>28} |")
print("=" * 145)
