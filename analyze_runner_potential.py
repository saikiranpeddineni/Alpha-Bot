import sqlite3
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
ET = timezone(timedelta(hours=-4))

conn = sqlite3.connect(str(MASTER_BARS))

# Target day: 2026-08-03 (13:30 UTC to 20:00 UTC)
start_e = int(datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc).timestamp())
end_e = int(datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc).timestamp())

symbols = ["CRWV", "NBIS", "TQQQ", "NVDA", "NOW", "AMZN", "META"]

print("=" * 100)
print(f"{'SYM':<6} {'ENTRY PX':<10} {'2R TARGET':<10} {'EXIT PX (2R)':<12} {'DAY HIGH':<10} {'MAX R ACHIEVED':<15} {'POTENTIAL P&L AT PEAK'}")
print("=" * 100)

trades_info = {
    "CRWV": {"entry": 78.5503, "risk": 1.40, "tp": 80.78, "qty": 260},
    "NBIS": {"entry": 199.4001, "risk": 3.61, "tp": 204.88, "qty": 103},
    "TQQQ": {"entry": 66.7398, "risk": 0.45, "tp": 67.12, "qty": 309},
    "NVDA": {"entry": 206.7900, "risk": 1.12, "tp": 208.25, "qty": 99},
}

for sym, info in trades_info.items():
    df = pd.read_sql(
        "SELECT ts, high, low, close FROM bars WHERE symbol=? AND tf='1Min' AND ts>=? AND ts<=? ORDER BY ts ASC",
        conn, params=(sym, start_e, end_e)
    )
    if not df.empty:
        day_high = df["high"].max()
        max_r = (day_high - info["entry"]) / info["risk"]
        max_pnl = (day_high - info["entry"]) * info["qty"]
        tp_r = (info["tp"] - info["entry"]) / info["risk"]
        
        print(f"{sym:<6} ${info['entry']:<9.2f} ${info['tp']:<9.2f} (R={tp_r:.1f}R)    ${day_high:<9.2f} {max_r:>5.2f}R           ${max_pnl:>8.2f}")

conn.close()
