import sqlite3
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
ET = timezone(timedelta(hours=-4))
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)

conn = sqlite3.connect(str(MASTER_BARS))

def simulate_runner_with_wide_breathing_room(sym, entry_px, risk, tp_px, sl_px, qty, entry_iso, scale_pct=0.70):
    entry_at = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
    start_e = int(entry_at.timestamp())
    end_e = int(MARKET_CLOSE_UTC.timestamp())
    
    df = pd.read_sql(
        "SELECT ts, open, high, low, close FROM bars WHERE symbol=? AND tf='1Min' AND ts>=? AND ts<=? ORDER BY ts ASC",
        conn, params=(sym, start_e, end_e)
    )
    if df.empty: return 0, 0, 0, 0, 0
    
    # 1. Scale portion (e.g. 70%) locked at Target TP
    qty_scaled = qty * scale_pct
    qty_runner = qty - qty_scaled
    pnl_scaled = (tp_px - entry_px) * qty_scaled
    
    # 2. Runner portion (30%) given wide breathing room (Stop placed at Breakeven + 0.2R)
    runner_stop = entry_px + (0.2 * risk)
    peak_high = tp_px
    runner_exit_px = None
    runner_exit_time = None
    
    # Start evaluating after target is hit
    target_hit_idx = 0
    for idx, row in df.iterrows():
        if row["high"] >= tp_px:
            target_hit_idx = idx
            break
            
    for idx in range(target_hit_idx + 1, len(df)):
        row = df.iloc[idx]
        h = row["high"]
        l = row["low"]
        c = row["close"]
        ts = datetime.fromtimestamp(row["ts"], tz=timezone.utc)
        
        if h > peak_high:
            peak_high = h
            # Only tighten stop once it exceeds 3R!
            if (peak_high - entry_px) / risk >= 3.0:
                candidate_stop = peak_high - (2.0 * risk)
                runner_stop = max(runner_stop, candidate_stop)
                
        if l <= runner_stop:
            runner_exit_px = runner_stop
            runner_exit_time = ts
            break
            
    if runner_exit_px is None:
        runner_exit_px = df.iloc[-1]["close"]
        runner_exit_time = datetime.fromtimestamp(df.iloc[-1]["ts"], tz=timezone.utc)
        
    pnl_runner = (runner_exit_px - entry_px) * qty_runner
    total_pnl = pnl_scaled + pnl_runner
    
    return pnl_scaled, pnl_runner, total_pnl, runner_exit_px, (runner_exit_px - entry_px) / risk

print("=" * 120)
print("  INSTITUTIONAL SCALE-OUT: 70% LOCKED AT 2R TARGET + 30% WIDE-BREATHING RUNNER TO 6R")
print("=" * 120)

trades = [
    {"sym": "CRWV", "entry": 78.5503, "risk": 1.40, "tp": 80.78, "sl": 77.15, "qty": 260, "ts": "2026-08-03T13:54:32.417387Z", "master_pnl": 592.34},
    {"sym": "NBIS", "entry": 199.4001, "risk": 3.61, "tp": 204.88, "sl": 195.79, "qty": 103, "ts": "2026-08-03T13:56:58.824707Z", "master_pnl": 580.91},
    {"sym": "TQQQ", "entry": 66.7398, "risk": 0.45, "tp": 67.12, "sl": 66.29, "qty": 309, "ts": "2026-08-03T14:47:04.757041Z", "master_pnl": 117.48},
]

tot_master = 0.0
tot_scaleout = 0.0

for t in trades:
    p_sc, p_rn, tot, r_px, r_mul = simulate_runner_with_wide_breathing_room(
        t["sym"], t["entry"], t["risk"], t["tp"], t["sl"], t["qty"], t["ts"], scale_pct=0.70
    )
    tot_master += t["master_pnl"]
    tot_scaleout += tot
    delta = tot - t["master_pnl"]
    print(f"[{t['sym']}] Entry: ${t['entry']:.2f} | 2R Target: ${t['tp']:.2f}")
    print(f"       Master Fixed 100% Exit:      ${t['master_pnl']:>8.2f}")
    print(f"       Scale 70% @ 2R + 30% Runner:  ${p_sc:>7.2f} (70% Target) + ${p_rn:>7.2f} (30% Runner @ ${r_px:.2f} / {r_mul:.1f}R) = ${tot:>8.2f} (DELTA: ${delta:>+8.2f})\n")

print(f"TOTAL MASTER ON 3 RUNNERS:    ${tot_master:>8.2f}")
print(f"TOTAL SCALE-OUT ON 3 RUNNERS: ${tot_scaleout:>8.2f}")
print(f"NET GAIN FROM RUNNER ENGINE:  ${tot_scaleout - tot_master:>+8.2f}")

conn.close()
