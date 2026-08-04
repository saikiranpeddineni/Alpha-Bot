import sqlite3
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
ET = timezone(timedelta(hours=-4))
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)

conn = sqlite3.connect(str(MASTER_BARS))

def simulate_hybrid_split(sym, entry_px, risk, tp_px, sl_px, qty, entry_iso):
    entry_at = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
    start_e = int(entry_at.timestamp())
    end_e = int(MARKET_CLOSE_UTC.timestamp())
    
    df = pd.read_sql(
        "SELECT ts, open, high, low, close FROM bars WHERE symbol=? AND tf='1Min' AND ts>=? AND ts<=? ORDER BY ts ASC",
        conn, params=(sym, start_e, end_e)
    )
    if df.empty: return 0, 0, 0
    
    # 50% locked at Target
    qty_t1 = qty * 0.5
    qty_t2 = qty - qty_t1
    pnl_t1 = (tp_px - entry_px) * qty_t1
    
    # Remaining 50% trailed with 1.5x ATR or Breakeven floor
    trail_stop = entry_px + (0.5 * risk)  # Lock in +0.5R profit on runner
    highest_price = tp_px
    runner_exit_px = None
    runner_exit_time = None
    runner_exit_reason = ""
    
    for idx, row in df.iterrows():
        c = row["close"]
        h = row["high"]
        l = row["low"]
        ts = datetime.fromtimestamp(row["ts"], tz=timezone.utc)
        
        if h > highest_price:
            highest_price = h
            # Trail stop at High - 1.5R or High - 20% of gains
            candidate_stop = highest_price - (1.2 * risk)
            trail_stop = max(trail_stop, candidate_stop)
            
        if l <= trail_stop:
            runner_exit_px = trail_stop
            runner_exit_time = ts
            runner_exit_reason = f"RUNNER_TRAIL_HIT (${trail_stop:.2f})"
            break
            
    if runner_exit_px is None:
        runner_exit_px = df.iloc[-1]["close"]
        runner_exit_time = datetime.fromtimestamp(df.iloc[-1]["ts"], tz=timezone.utc)
        runner_exit_reason = "EOD_CLOSE"
        
    pnl_t2 = (runner_exit_px - entry_px) * qty_t2
    total_pnl = pnl_t1 + pnl_t2
    
    return pnl_t1, pnl_t2, total_pnl, runner_exit_px, runner_exit_time, runner_exit_reason

print("=" * 120)
print("  HYBRID SCALE-OUT: 50% LOCKED AT 2R TARGET + 50% TRAILED TO 6R RUNNER")
print("=" * 120)

trades = [
    {"sym": "CRWV", "entry": 78.5503, "risk": 1.40, "tp": 80.78, "sl": 77.15, "qty": 260, "ts": "2026-08-03T13:54:32.417387Z", "master_pnl": 592.34},
    {"sym": "NBIS", "entry": 199.4001, "risk": 3.61, "tp": 204.88, "sl": 195.79, "qty": 103, "ts": "2026-08-03T13:56:58.824707Z", "master_pnl": 580.91},
    {"sym": "TQQQ", "entry": 66.7398, "risk": 0.45, "tp": 67.12, "sl": 66.29, "qty": 309, "ts": "2026-08-03T14:47:04.757041Z", "master_pnl": 117.48},
]

for t in trades:
    p1, p2, tot, exit_px, exit_time, reason = simulate_hybrid_split(
        t["sym"], t["entry"], t["risk"], t["tp"], t["sl"], t["qty"], t["ts"]
    )
    delta = tot - t["master_pnl"]
    print(f"[{t['sym']}] Entry: ${t['entry']:.2f} | 2R Target: ${t['tp']:.2f}")
    print(f"       Master Fixed 100% Exit:   Realized P&L = ${t['master_pnl']:>8.2f}")
    print(f"       Hybrid 50/50 Scale-Out:   T1 (50% @ 2R): ${p1:>7.2f}  |  T2 (50% Runner): ${p2:>7.2f} (@ ${exit_px:.2f})")
    print(f"       TOTAL REALIZED P&L:       ${tot:>8.2f}  (DELTA: ${delta:>+8.2f} / {(tot/t['master_pnl'] - 1.0)*100:>+5.1f}%)\n")

conn.close()
