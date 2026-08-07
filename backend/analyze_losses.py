import sqlite3, json, datetime
from dateutil import parser
import pandas as pd

# 1. Fetch worst trades
conn_alpha = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c_alpha = conn_alpha.cursor()
c_alpha.execute('SELECT payload, exit_at, pnl FROM trades WHERE exit_at >= ? ORDER BY pnl ASC LIMIT 5', ((datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat(),))
trades = []
for row in c_alpha.fetchall():
    data = json.loads(row[0])
    trades.append({
        "symbol": data.get("symbol"),
        "side": data.get("side"),
        "qty": data.get("qty"),
        "entry_price": data.get("entry_price"),
        "exit_price": data.get("exit_price"),
        "pnl": row[2],
        "exit_time": row[1],
        "exit_reason": data.get("exit_reason", "unknown")
    })

# 2. Fetch bars for these trades
conn_bars = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/bars_cache.db')
c_bars = conn_bars.cursor()

results = []
for t in trades:
    exit_dt = parser.parse(t["exit_time"])
    ts_exit_s = int(exit_dt.timestamp())
    
    # tf is '1Min' not '1m'
    c_bars.execute('''
        SELECT ts, open, high, low, close, volume 
        FROM bars 
        WHERE symbol = ? AND tf = '1Min' AND ts >= ? AND ts <= ?
        ORDER BY ts ASC
    ''', (t["symbol"], ts_exit_s - 600, ts_exit_s + 120))
    
    bars = []
    for b in c_bars.fetchall():
        dt = datetime.datetime.fromtimestamp(b[0], datetime.UTC).strftime('%H:%M')
        bars.append({"time": dt, "open": b[1], "high": b[2], "low": b[3], "close": b[4], "vol": b[5]})
        
    t["bars"] = bars
    results.append(t)

with open('c:/Users/peddi/Desktop/Bots/Bot_QA/backend/loss_analysis.json', 'w') as f:
    json.dump(results, f, indent=2)
print("Done")
