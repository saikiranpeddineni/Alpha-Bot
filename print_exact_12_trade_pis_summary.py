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

print("=" * 140)
print(f"{'#':<3} {'SYM':<5} {'STRATEGY':<14} {'SIDE':<5} {'ENTRY':<8} {'EXIT PX':<9} {'MASTER P&L':<12} {'PIS P&L':<10} {'DELTA':<10} {'PIS EXIT TIME & REASON'}")
print("=" * 140)

tot_master = 0.0
tot_pis = 0.0

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

    config = PISConfig()
    row0 = bars_from_entry.iloc[0]
    ts0 = bars_from_entry.index[0].to_pydatetime()
    if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)

    obs0 = PositionObservation(
        timestamp=ts0, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
        current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
        volume=float(row0["volume"]), atr=1.0, initial_stop=initial_stop, risk_per_share=risk_per_share,
        current_stop=initial_stop, take_profit=target_tp, r_multiple=0.0, news_sentiment_score=sentiment_score
    )

    brain = PositionBrain(sym, obs0, config)
    current_stop = initial_stop
    exit_price = None
    exit_at = None
    exit_reason = None

    for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
        bar_ts = bar_ts_pd.to_pydatetime()
        if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)

        bar_idx = bars.index.get_loc(bar_ts_pd)
        atr = estimate_atr(bars, bar_idx)
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        r_mul = (close - entry_price) * side_float / risk_per_share

        obs = PositionObservation(
            timestamp=bar_ts, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
            current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr,
            initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=current_stop,
            take_profit=target_tp, r_multiple=r_mul, news_sentiment_score=sentiment_score
        )

        if i > 0:
            if side_float == 1.0 and low <= current_stop:
                exit_price = current_stop
                exit_at = bar_ts
                exit_reason = f"STOP_BREACH (${current_stop:.2f})"
                break
            elif side_float == -1.0 and high >= current_stop:
                exit_price = current_stop
                exit_at = bar_ts
                exit_reason = f"STOP_BREACH (${current_stop:.2f})"
                break

        decision = brain.observe(obs)
        action_name = decision.action.value if hasattr(decision.action, "value") else str(decision.action)
        reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]

        if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
            if side_float > 0 and decision.target_stop_loss > current_stop:
                current_stop = decision.target_stop_loss
            elif side_float < 0 and decision.target_stop_loss < current_stop:
                current_stop = decision.target_stop_loss

        if decision.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
            exit_price = close if "PROTECTION_BREACH" not in reasons else current_stop
            exit_at = bar_ts
            exit_reason = f"{action_name} ({','.join(reasons)})"
            break

    brain.close()

    realized_pnl = round((exit_price - entry_price) * side_float * qty, 2)
    master_pnl = trade["legacy_pnl"]
    delta = realized_pnl - master_pnl
    tot_master += master_pnl
    tot_pis += realized_pnl

    exit_time_str = exit_at.astimezone(ET).strftime("%H:%M:%S") if exit_at else "N/A"
    print(f"{idx:<3} {sym:<5} {trade['strategy']:<14} {side_str:<5} ${entry_price:<7.2f} ${exit_price:<8.2f} ${master_pnl:>9.2f}  ${realized_pnl:>8.2f}  ${delta:>+8.2f}  {exit_time_str} ET - {exit_reason}")

bars_conn.close()
print("=" * 140)
print(f"TOTAL: MASTER BOT = ${tot_master:.2f} | PURE PRODUCTION PIS = ${tot_pis:.2f} | VARIANCE = ${tot_pis - tot_master:+.2f}")
print("=" * 140)
