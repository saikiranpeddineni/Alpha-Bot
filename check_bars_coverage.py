import sqlite3
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")

conn = sqlite3.connect(str(MASTER_BARS))
df = pd.read_sql("SELECT min(ts) as min_ts, max(ts) as max_ts, count(*) as cnt, count(distinct symbol) as sym_cnt FROM bars", conn)
min_dt = datetime.fromtimestamp(df['min_ts'].iloc[0], tz=timezone.utc)
max_dt = datetime.fromtimestamp(df['max_ts'].iloc[0], tz=timezone.utc)

print(f"Bars cache range: {min_dt} to {max_dt}")
print(f"Total bar count: {df['cnt'].iloc[0]} across {df['sym_cnt'].iloc[0]} symbols")

# Check distinct dates in bars
df_dates = pd.read_sql("SELECT date(datetime(ts, 'unixepoch')) as d, count(*) as bar_count FROM bars GROUP BY d ORDER BY d DESC LIMIT 30", conn)
print("\nDates present in bars_cache.db:")
print(df_dates)

conn.close()
