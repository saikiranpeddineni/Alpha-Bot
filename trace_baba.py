import sqlite3
import json
import pandas as pd
from datetime import datetime

MASTER_BARS = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db"

conn_bars = sqlite3.connect(MASTER_BARS)
dt = datetime.fromisoformat("2026-08-03T13:42:18+00:00")
start_ts = dt.timestamp() - 7200
end_ts = dt.timestamp() + 18000

df_bars = pd.read_sql("SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = 'BABA' AND ts >= ? AND ts <= ? ORDER BY ts ASC", conn_bars, params=(start_ts, end_ts))
conn_bars.close()

df_bars["datetime_utc"] = pd.to_datetime(df_bars["ts"], unit="s", utc=True)
df_bars["datetime_et"] = df_bars["datetime_utc"].dt.tz_convert("America/New_York")

entry_price = 128.60
risk = 0.65
side = 1.0

for i, r in df_bars.head(20).iterrows():
    c = float(r["close"])
    h = float(r["high"])
    l = float(r["low"])
    r_mul = (c - entry_price) * side / risk
    bar_range = h - l
    t_str = r["datetime_et"].strftime("%H:%M:%S")
    print(f"Bar {i} [{t_str}]: Close=${c:.2f}, High=${h:.2f}, Low=${l:.2f}, Range=${bar_range:.2f}, R={r_mul:+.2f}")
