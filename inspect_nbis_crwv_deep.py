import sqlite3
import json
import pandas as pd
from datetime import datetime

MASTER_DB = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
MASTER_BARS = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db"

conn_bars = sqlite3.connect(MASTER_BARS)

def inspect_symbol(sym: str, start_iso: str, entry_price: float, side: float, risk: float):
    dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    start_ts = dt.timestamp() - 60
    end_ts = dt.timestamp() + 18000

    df_bars = pd.read_sql(
        "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
        conn_bars,
        params=(sym, start_ts, end_ts)
    )
    if df_bars.empty:
        print(f"No bars found for {sym}")
        return

    df_bars["datetime_utc"] = pd.to_datetime(df_bars["ts"], unit="s", utc=True)
    df_bars["datetime_et"] = df_bars["datetime_utc"].dt.tz_convert("America/New_York")

    print("=" * 110)
    print(f"                                   {sym} EXACT BAR TRACE")
    print("=" * 110)
    print(f"{'INDEX':<6} {'TIME (ET)':<10} {'OPEN':<9} {'HIGH':<9} {'LOW':<9} {'CLOSE':<9} {'VOL':<8} {'BAR RANGE':<10} {'R-MULT':<8} {'PEAK R'}")
    print("-" * 110)
    peak_r = 0.0
    for i, (_, r) in enumerate(df_bars.iterrows()):
        c = float(r["close"])
        h = float(r["high"])
        l = float(r["low"])
        o = float(r["open"])
        v = int(r["volume"])
        bar_range = h - l
        r_mul = (c - entry_price) * side / risk
        cur_peak = (h - entry_price) / risk if side > 0 else (entry_price - l) / risk
        peak_r = max(peak_r, cur_peak)
        t_str = r["datetime_et"].strftime("%H:%M:%S")
        print(f"{i:<6} {t_str:<10} ${o:<8.2f} ${h:<8.2f} ${l:<8.2f} ${c:<8.2f} {v:<8} ${bar_range:<9.2f} {r_mul:>+6.2f}R  {peak_r:>+6.2f}R")
        if i >= 35:
            break

print("Inspecting NBIS:")
inspect_symbol("NBIS", "2026-08-03T13:31:07.729707+00:00", 199.40, 1.0, 2.95)

print("\nInspecting CRWV:")
inspect_symbol("CRWV", "2026-08-03T13:38:15.540183+00:00", 78.55, 1.0, 0.92)

conn_bars.close()
