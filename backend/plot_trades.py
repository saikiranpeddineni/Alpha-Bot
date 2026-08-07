import sqlite3, json, datetime
import matplotlib.pyplot as plt
import pandas as pd
from dateutil import parser
import os

conn_alpha = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c_alpha = conn_alpha.cursor()
c_alpha.execute('SELECT payload, exit_at, pnl FROM trades WHERE exit_at >= ? ORDER BY pnl ASC LIMIT 5', ((datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24)).isoformat(),))

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
        "entry_at": data.get("entry_at"),
        "exit_at": row[1]
    })

conn_bars = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/bars_cache.db')
c_bars = conn_bars.cursor()

artifact_dir = "C:/Users/peddi/.gemini/antigravity-ide/brain/3dbec354-c949-4690-8a49-0034f7e0a741"

for i, t in enumerate(trades):
    if not t.get("entry_at"):
        continue
    
    entry_dt = parser.parse(t["entry_at"]).replace(tzinfo=datetime.UTC)
    exit_dt = parser.parse(t["exit_at"]).replace(tzinfo=datetime.UTC)
    
    ts_entry = int(entry_dt.timestamp())
    ts_exit = int(exit_dt.timestamp())
    
    c_bars.execute('''
        SELECT ts, close
        FROM bars 
        WHERE symbol = ? AND tf = '1Min' AND ts >= ? AND ts <= ?
        ORDER BY ts ASC
    ''', (t["symbol"], ts_entry - 1800, ts_exit + 1800))
    
    rows = c_bars.fetchall()
    if not rows:
        continue
        
    df = pd.DataFrame(rows, columns=['ts', 'close'])
    df['dt'] = pd.to_datetime(df['ts'], unit='s', utc=True)
    
    plt.figure(figsize=(10, 5))
    plt.plot(df['dt'], df['close'], label=f"{t['symbol']} Price", color='blue')
    
    # Mark entry and exit
    plt.axvline(x=entry_dt, color='green', linestyle='--', label=f"Entry ({t['side'].upper()})")
    plt.axvline(x=exit_dt, color='red', linestyle='--', label="Exit")
    
    plt.scatter([entry_dt], [t['entry_price']], color='green', zorder=5, s=100)
    plt.scatter([exit_dt], [t['exit_price']], color='red', zorder=5, s=100)
    
    plt.title(f"{t['symbol']} Trade Breakdown (PnL: ${t['pnl']:.2f})")
    plt.xlabel('Time (UTC)')
    plt.ylabel('Price')
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(f"{artifact_dir}/trade_{i}_{t['symbol']}.png")
    plt.close()

print("Charts generated!")
