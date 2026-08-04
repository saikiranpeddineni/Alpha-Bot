import sqlite3
import pandas as pd
from datetime import datetime, timezone

bars_db = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db"
conn = sqlite3.connect(bars_db)
cur = conn.cursor()

symbols = ['INTC', 'CRWV', 'NBIS', 'NOW', 'SPCX', 'META', 'TQQQ', 'AMZN', 'BABA', 'NVDA']

start_ts = int(datetime(2026, 8, 3, 13, 0, 0, tzinfo=timezone.utc).timestamp())
end_ts = int(datetime(2026, 8, 3, 20, 30, 0, tzinfo=timezone.utc).timestamp())

for sym in symbols:
    cur.execute("SELECT count(*), min(ts), max(ts) FROM bars WHERE symbol=? AND tf='1Min' AND ts>=? AND ts<=?", (sym, start_ts, end_ts))
    row = cur.fetchone()
    min_dt = datetime.fromtimestamp(row[1], tz=timezone.utc).strftime("%H:%M:%S") if row[1] else "None"
    max_dt = datetime.fromtimestamp(row[2], tz=timezone.utc).strftime("%H:%M:%S") if row[2] else "None"
    print(f"Symbol {sym:<6}: {row[0]} bars from {min_dt} UTC to {max_dt} UTC")

conn.close()
